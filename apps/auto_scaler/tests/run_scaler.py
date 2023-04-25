from argparse import ArgumentParser, FileType
from os import environ
from pathlib import Path

import yaml

from auto_scaler import AutoScaler
from aws_driver import AWSDriver
from gcp_driver import GCPDriver
from clearml.automation.auto_scaler import ScalerConfig

drivers = {
    'aws': AWSDriver,
    'gcp': GCPDriver,
}

# Support --help
parser = ArgumentParser()
parser.add_argument('driver', choices=['aws', 'gcp'])
parser.add_argument('--config-file', '-c', type=FileType('r'))
args = parser.parse_args()

if args.config_file:
    cfg_file_name = args.config_file.name
else:
    cfg_file_name = environ.get('CONFIG_FILE')

if not cfg_file_name:
    raise SystemExit('error: not config file, either pass --config-file or set CONFIG_FILE')


cfg_file = Path(cfg_file_name)
if not cfg_file.is_file():
    raise SystemExit(f'error: {cfg_file!r} does not exist')

with cfg_file.open() as fp:
    config = yaml.safe_load(fp)

Driver = drivers.get(args.driver)
if Driver is None:
    raise SystemError('error: unknown driver - {args.driver!r}')

driver = Driver.from_config(config)
scaler_config = ScalerConfig.from_config(config)
scaler = AutoScaler(scaler_config, driver)
scaler.start()
