import re
from base64 import b64decode
from threading import Thread
from time import sleep
from unittest.mock import MagicMock

import clearml.automation.aws_driver as cml_driver
from clearml import Task
from clearml.automation.auto_scaler import ScalerConfig

import auto_scaler
import aws_driver

queue = MagicMock(id='q1', name='qfirst')
prefix = 'test'
resource = 'm1'
settings = {
    'workers_prefix': prefix,
}
queue_worker_count = 3

config = {
    'configurations': {
        'extra_clearml_conf': '',
        'extra_trains_conf': '',
        'extra_vm_bash_script': '',
        'queues': {queue.name: [[resource, queue_worker_count]]},
        'resource_configurations': {
            resource: {
                'ami_id': 'ami-04c0416d6bd8e4b1f',
                'availability_zone': 'us-east-1b',
                'ebs_device_name': '/dev/sda1',
                'ebs_volume_size': 100,
                'ebs_volume_type': 'gp3',
                'instance_type': 'g4dn.4xlarge',
                'is_spot': True,
                'key_name': None,
                'security_group_ids': None
            },
        },
    },
    'hyper_params': {
     'default_docker_image': None,
     'git_pass': None,
     'git_user': None,
     'max_idle_time_min': 1,
     'max_spin_up_time_min': 30,
     'polling_interval_time_min': 0.1,
     'workers_prefix': prefix,
     'cloud_credentials_key': 'joe',
     'cloud_credentials_secret': 'baz00ka',
     'cloud_credentials_region': 'us-east-1',
    },
}


def new_mock_api(worker=None):
    mock_api = MagicMock()
    if worker is None:
        wid = '{}:{}:type1:inst1'.format(prefix, resource)
        worker = MagicMock(id=wid)
    mock_api.queues.get_all.return_value = [queue]
    mock_api.workers.get_all.return_value = [worker]
    mock_api.queues.get_by_id.return_value = MagicMock(entries=['e1', 'e2', 'e2'])
    return mock_api


# Idea for not mocked test: Run a task that will spin a process which terminates the spot instance
# Need to find a way to do it only once
def test_spot_crash(monkeypatch):
    cloud_id = 'i-07cf7d6750455cb62'
    # Set boto instance state to be terminated
    mock_boto = MagicMock()
    mock_boto.resource.return_value = mock_boto  # re-use mock as ec2 resource
    mock_boto.client.return_value = mock_boto  # re-use mock as ec2 client
    mock_boto.describe_spot_instance_requests.return_value = {
        'SpotInstanceRequests': [
            {'InstanceId': cloud_id},
        ]
    }

    mock_boto.Instance.return_value = MagicMock(state={'Name': 'terminated'})

    typ = config['configurations']['resource_configurations'][resource]['instance_type']
    wid = '{}:{}:{}:{}'.format(prefix, resource, typ, cloud_id)
    worker = MagicMock(
        id=wid,
        task=MagicMock(status=Task.TaskStatusEnum.in_progress),
    )
    mock_api = new_mock_api(worker)
    mock_api.tasks.get_by_id.return_value = MagicMock(status=Task.TaskStatusEnum.in_progress)

    monkeypatch.setattr(aws_driver, 'boto3', mock_boto)
    monkeypatch.setattr(cml_driver, 'boto3', mock_boto)
    monkeypatch.setattr(aws_driver, 'spot_check_interval', 0.01)

    drv = aws_driver.AWSDriver.from_config(config)
    scaler = auto_scaler.AutoScaler(ScalerConfig.from_config(config), drv)
    scaler.api_client = mock_api

    # scaler.start()  # Uncomment this when debugging
    Thread(target=scaler.start, daemon=True).start()
    sleep(15)
    scaler.stop()

    found = False
    for call in mock_boto.request_spot_instances.call_args_list:
        user_data = b64decode(call[1]['LaunchSpecification'].UserData).decode()
        if re.search('clearml_agent.*--id', user_data):
            found = True
    assert found, 'no restart'
