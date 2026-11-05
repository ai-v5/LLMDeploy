"""Run the accounting hop or the local read-only dashboard."""
import argparse
from pathlib import Path

from .proxy import Proxy
from .web import Dashboard


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    proxy = commands.add_parser('proxy')
    proxy.add_argument('--port', type=int, default=19882)
    proxy.add_argument('--upstream-port', type=int, required=True)
    proxy.add_argument('--database', type=Path, required=True)
    dashboard = commands.add_parser('dashboard')
    dashboard.add_argument('--port', type=int, default=19881)
    dashboard.add_argument('--database', type=Path, required=True)
    dashboard.add_argument('--backend-port', type=int, action='append', default=[])
    dashboard.add_argument('--telemetry-port', type=int)
    dashboard.add_argument('--motor-management-port', type=int, action='append', default=[])
    dashboard.add_argument('--motor-history-database', type=Path)
    args = parser.parse_args()
    server = (Proxy(args.port, args.upstream_port, args.database) if args.command == 'proxy'
              else Dashboard(args.port, args.database, args.backend_port, args.telemetry_port,
                             motor_management_ports=args.motor_management_port,
                             motor_history_path=args.motor_history_database))
    print(f'Listening on http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
