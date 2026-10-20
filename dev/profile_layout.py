"""Offline userdata migration; stop the addon service before running this tool."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from resources.lib.profile_migration import LOCATIONS, plan, migrate, rollback, _project_key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile', type=Path)
    parser.add_argument('--backup', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--rollback', action='store_true')
    args = parser.parse_args()
    if args.rollback:
        rollback(args.backup)
        print('Layout rollback complete')
    elif args.apply:
        print(json.dumps(migrate(args.profile, args.backup)))
    else:
        print(json.dumps([[str(a.relative_to(args.profile)), str(b.relative_to(args.profile))]
                          for a, b in plan(args.profile)]))


if __name__ == '__main__':
    main()
