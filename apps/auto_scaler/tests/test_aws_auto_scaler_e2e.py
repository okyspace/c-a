import logging
import re
from datetime import datetime, timedelta
from os import environ
from pathlib import Path
from socket import gethostname
from subprocess import Popen, run
from sys import executable, stderr
from time import sleep

import boto3
import pytest

from clearml import Task
from clearml.automation.auto_scaler import WorkerId
from clearml.backend_api.session.client import APIClient

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)

access_key = environ.get('AWS_ACCESS_KEY_ID')
secret_key = environ.get('AWS_SECRET_ACCESS_KEY')

if not (access_key and secret_key):
    logging.info('No AWS credentials found, skipping', file=stderr)
    pytestmark = pytest.mark.skip


test_dir = Path(__file__).absolute().parent
done_statuses = {
    'completed',
    'failed',
    'stopped',
}


def git_root():
    cmd = ['git', 'rev-parse', '--show-toplevel']
    out = run(cmd, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def parse_task_id(text):
    match = re.search(r'task id=([a-f0-9]+)', text, re.MULTILINE)
    assert match, f'cannot find task ID in:\n{text}'
    return match[1]


def gen_config(root, queue, prefix):
    template_file = test_dir / 'aws_autoscaler.yaml'
    with template_file.open() as fp:
        template = fp.read()

    cfg = template.format(
        AWS_ACCESS_KEY_ID=access_key,
        AWS_SECRET_ACCESS_KEY=secret_key,
        QUEUE=queue,
        WORKERS_PREFIX=prefix,
    )

    cfg_file = root / 'aws_autoscaler.yml'
    with cfg_file.open('w') as out:
        out.write(cfg)

    return cfg_file


def clone_task_repo(task_dir):
    # TODO: Move the repo under clearml
    cmd = ['git', 'clone', 'https://github.com/allegroai/painless.git', str(task_dir)]
    logging.info('CMD: %s', ' '.join(cmd))
    run(cmd, check=True)


def append_py_path(env, path):
    py_path = env.get('PYTHONPATH')
    py_path = f'{path}:{py_path}' if py_path else str(path)
    env['PYTHONPATH'] = py_path


def run_task(root_dir, queue, env):
    task_dir = root_dir / 'painless'
    clone_task_repo(task_dir)
    cmd = [executable, 'painless.py', '--queue', queue]
    logging.info('CMD: %s', ' '.join(cmd))
    out = run(cmd, cwd=str(task_dir), env=env, capture_output=True, text=True)
    return parse_task_id(out.stdout)


@pytest.fixture
def cleanup():
    funcs = []
    yield funcs

    for fn in reversed(funcs):
        try:
            logging.info('cleanup %s', fn.__name__)
            fn()
        except Exception as err:
            logging.warnings('error cleanup %s: %s', fn.__name__, err)


def run_scaler(env):
    runner = test_dir / 'run_scaler.py'
    cmd = [executable, str(runner), 'aws']
    logging.info('CMD: %s', ' '.join(cmd))
    return Popen(cmd, env=env)


def gen_prefix():
    host = gethostname()
    time = datetime.now().strftime('%Y%m%dT%H%M%S')
    return f'aws_autoscaler_test_{host}_{time}'


def wait_for_task(task_id, timeout):
    start = datetime.now()
    while datetime.now() - start <= timeout:
        status = Task.get_task(task_id).status
        logging.info('Task %s: %s', task_id, status)
        if status in done_statuses:
            return status
        sleep(10)
    return None


def clean_instances(prefix):
    client = APIClient()
    instance_ids = []
    for worker in client.workers.get_all():
        try:
            wid = WorkerId(worker.id)
        except ValueError as err:
            logging.warning('ignoring malformed worker_id: %r (%s)', worker.id, err)
            continue

        if wid.prefix != prefix:
            continue
        instance_ids.append(wid.cloud_id)

    logging.info('%s instance_ids: %r', prefix, instance_ids)
    if not instance_ids:
        return

    ec2 = boto3.client('ec2')
    response = ec2.terminate_instances(InstanceIds=instance_ids)
    logging.info('ec2 response: %r', response)


def delete_queue(name):
    client = APIClient()
    for queue in client.queues.get_all():
        if queue.name != name:
            continue
        client.queues.delete(queue.id)
        return

    logging.warning('cannot find queue %r to clean', name)


def test_spot_crash(tmp_path, cleanup):
    logging.info('TEMP DIR: %s', tmp_path)

    queue = prefix = gen_prefix()
    cleanup.append(lambda: clean_instances(prefix))
    cleanup.append(lambda: delete_queue(queue))

    cfg_file = gen_config(tmp_path, queue, prefix)

    env = environ.copy()
    env['CONFIG_FILE'] = str(cfg_file)
    append_py_path(env, git_root())

    proc = run_scaler(env)
    cleanup.append(proc.kill)

    task_id = run_task(tmp_path, queue, env)

    timeout = timedelta(minutes=30)
    status = wait_for_task(task_id, timeout)
    assert status, f'task {task_id} not done after {timeout}'
    assert status == 'completed', f'bad status: {status}'
