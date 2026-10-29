"""CLI: python -m cbioportal_studies <method> key=value ... — JSON output."""
import json
import sys

from .tool import CBioPortalStudies


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python -m cbioportal_studies <method> key=value ...",
              file=sys.stderr)
        return 2
    method, *pairs = argv





    INT_PARAMS = {"max_records", "entrez_gene_id"}
    LIST_PARAMS = {"study_ids"}
    kwargs = {}
    for p in pairs:
        k, _, v = p.partition("=")
        if k in INT_PARAMS:
            v = int(v)
        elif k in LIST_PARAMS:
            v = [s for s in v.split(",") if s]
        kwargs[k] = v
    tool = CBioPortalStudies()
    fn = getattr(tool, method, None)
    if fn is None:
        print(f"unknown method {method}", file=sys.stderr)
        return 2
    print(json.dumps(fn(**kwargs), indent=2, sort_keys=True,
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
