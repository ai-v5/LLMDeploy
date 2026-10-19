"""Register the bundled stat logger on PYTHONPATH without pip/network access."""
import argparse
from pathlib import Path


def register(target):
    directory = Path(target) / 'llmdeploy_usage-1.0.dist-info'
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'METADATA').write_text('Metadata-Version: 2.1\nName: llmdeploy-usage\nVersion: 1.0\n', encoding='utf-8')
    (directory / 'entry_points.txt').write_text(
        '[vllm.stat_logger_plugins]\nllmdeploy_usage = deploy_usage.plugin:UsageLogger\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True)
    register(parser.parse_args().target)
