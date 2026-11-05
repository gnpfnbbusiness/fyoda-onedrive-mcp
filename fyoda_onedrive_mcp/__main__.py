import json
import sys

USAGE = """usage: python -m fyoda_onedrive_mcp <command>
  serve                      run the MCP server over stdio (default)
  login                      sign in with a personal Microsoft account (device code)
  whoami | ls [path] | search <text> | meta <ref> | read <ref> [offset] | download <ref> [dir]"""


def main(argv=None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    cmd = a.pop(0) if a else "serve"
    try:
        if cmd == "serve":
            from .server import run
            run()
            return 0
        if cmd == "login":
            from .auth import Auth
            print("Signed in as:", Auth().login())
            return 0
        from . import core
        ops = {
            "whoami": lambda: core.whoami(),
            "ls": lambda: core.list_folder(a[0] if a else "/"),
            "search": lambda: core.search(" ".join(a)),
            "meta": lambda: core.metadata(a[0]),
            "read": lambda: core.read_text(a[0], int(a[1]) if len(a) > 1 else 0),
            "download": lambda: core.download(a[0], a[1] if len(a) > 1 else None),
        }
        if cmd not in ops:
            print(USAGE, file=sys.stderr)
            return 2
        print(json.dumps(ops[cmd](), indent=2, ensure_ascii=False))
        return 0
    except Exception as exc:  # concise error, never a token
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
