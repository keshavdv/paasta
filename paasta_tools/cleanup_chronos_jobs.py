#!/usr/bin/env python
# Copyright 2015 Yelp Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Usage: ./cleanup_chronos_jobs.py [options]

Clean up chronos jobs that aren't supposed to run on this cluster by deleting them.

Gets the current job list from chronos, and then a 'valid_job_list'
via chronos_tools.get_chronos_jobs_for_cluster

If a job is deployed by chronos but not in the expected list, it is deleted.
Any tasks associated with that job are also deleted.

- -d <SOA_DIR>, --soa-dir <SOA_DIR>: Specify a SOA config dir to read from
"""
import argparse
import sys

import pysensu_yelp
import service_configuration_lib

from paasta_tools import chronos_tools
from paasta_tools.check_chronos_jobs import send_event
from paasta_tools.utils import InvalidJobNameError


def parse_args():
    parser = argparse.ArgumentParser(description='Cleans up stale chronos jobs.')
    parser.add_argument('-d', '--soa-dir', dest="soa_dir", metavar="SOA_DIR",
                        default=service_configuration_lib.DEFAULT_SOA_DIR,
                        help="define a different soa config directory")
    args = parser.parse_args()
    return args


def execute_chronos_api_call_for_job(api_call, job):
    """Attempt a call to the Chronos api, catching any exception.

    We *have* to catch Exception, because the client catches
    the more specific exception thrown by the http clients
    and rethrows an Exception -_-.

    The chronos api returns a 204 No Content when the delete is
    successful, and chronos-python only returns the body of the
    response from all http calls. So, if this is successful,
    then None will be returned.
    https://github.com/asher/chronos-python/pull/7

    We catch it here, so that the other deletes are completed.
    """
    try:
        return api_call(job)
    except Exception as e:
        return e


def cleanup_jobs(client, jobs):
    """Maps a list of jobs to cleanup to a list of response objects (or exception objects) from the api"""
    return [(job, execute_chronos_api_call_for_job(client.delete, job)) for job in jobs]


def cleanup_tasks(client, jobs):
    """Maps a list of tasks to cleanup to a list of response objects (or exception objects) from the api"""
    return [(job, execute_chronos_api_call_for_job(client.delete_tasks, job)) for job in jobs]


def format_list_output(title, job_names):
    return '%s\n  %s' % (title, '\n  '.join(job_names))


def deployed_job_names(client):
    return [job['name'] for job in client.list()]


def filter_paasta_jobs(jobs):
    """
    Given a list of job name strings, return only those in the format PaaSTA expects.

    :param jobs: a list of job names.
    :returns: those job names in a format PaaSTA expects
    """
    formatted = []
    for job in jobs:
        try:
            # attempt to decompose it
            chronos_tools.decompose_job_id(job)
            formatted.append(job)
        except InvalidJobNameError:
            pass
    return formatted


def main():

    args = parse_args()
    soa_dir = args.soa_dir

    config = chronos_tools.load_chronos_config()
    client = chronos_tools.get_chronos_client(config)

    # get_chronos_jobs_for_cluster returns (service, job)
    expected_service_jobs = set([chronos_tools.compose_job_id(*job) for job in
                                 chronos_tools.get_chronos_jobs_for_cluster(soa_dir=args.soa_dir)])

    running_service_jobs = set(deployed_job_names(client))

    to_delete = running_service_jobs - expected_service_jobs

    task_responses = cleanup_tasks(client, to_delete)
    task_successes = []
    task_failures = []
    for response in task_responses:
        if isinstance(response[-1], Exception):
            task_failures.append(response)
        else:
            task_successes.append(response)

    job_responses = cleanup_jobs(client, to_delete)
    job_successes = []
    job_failures = []
    for response in job_responses:
        if isinstance(response[-1], Exception):
            job_failures.append(response)
        else:
            job_successes.append(response)
            try:
                (service, instance) = chronos_tools.decompose_job_id(response[0])
                send_event(
                    service=service,
                    instance=instance,
                    monitoring_overrides={},
                    soa_dir=soa_dir,
                    status_code=pysensu_yelp.Status.OK,
                    message="This instance was removed and is no longer supposed to be scheduled.",
                )
            except InvalidJobNameError:
                # If we deleted some bogus job with a bogus jobid that could not be parsed,
                # Just move on, no need to send any kind of paasta event.
                pass

    if len(to_delete) == 0:
        print 'No Chronos Jobs to remove'
    else:
        if len(task_successes) > 0:
            print format_list_output("Successfully Removed Tasks (if any were running) for:",
                                     [job[0] for job in task_successes])

        # if there are any failures, print and exit appropriately
        if len(task_failures) > 0:
            print format_list_output("Failed to Delete Tasks for:", [job[0] for job in task_failures])

        if len(job_successes) > 0:
            print format_list_output("Successfully Removed Jobs:", [job[0] for job in job_successes])

        # if there are any failures, print and exit appropriately
        if len(job_failures) > 0:
            print format_list_output("Failed to Delete Jobs:", [job[0] for job in job_failures])

        if len(job_failures) > 0 or len(task_failures) > 0:
            sys.exit(1)

if __name__ == "__main__":
    main()
