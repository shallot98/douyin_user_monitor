"""Check that a local file has the same bytes as an existing D2 file.

The mover deletes local sources once the target exists. Size alone cannot prove
the bytes match, so for D2 targets it compares the local content identity with
the one D2 reports. Exit 0 = same content, 3 = different, 1 = could not verify.
"""

import argparse
import json
from pathlib import Path

from .d2_cid import file_content_identity
from .d2_client import D2Client, D2Error, HttpTransport
from .d2_upload import GET_FILE_ENDPOINT, _load_credentials_from_openlist_db, _validate_destination

EXIT_MATCH = 0
EXIT_UNVERIFIED = 1
EXIT_MISMATCH = 3


def verify_file(client, source_path, destination):
    _validate_destination(destination)
    data = client.post(GET_FILE_ENDPOINT, {"path": destination})
    remote = data.get("content_identity") if isinstance(data, dict) else None
    if not isinstance(remote, str) or not remote:
        raise D2Error("D2 file has no content identity")
    with Path(source_path).open("rb") as source:
        local = file_content_identity(source)
    return {"match": local == remote, "local": local, "remote": remote}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--openlist-db", required=True, type=Path)
    parser.add_argument("--mount-path", default="/d2")
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--path", required=True, help="Destination path native to D2 (without /d2 prefix)")
    args = parser.parse_args(argv)
    try:
        client = D2Client(_load_credentials_from_openlist_db(args.openlist_db, args.mount_path),
                          transport=HttpTransport())
        result = verify_file(client, args.file, args.path)
    except (D2Error, OSError) as exc:
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return EXIT_UNVERIFIED
    print(json.dumps(result, ensure_ascii=False))
    return EXIT_MATCH if result["match"] else EXIT_MISMATCH


if __name__ == "__main__":
    raise SystemExit(main())
