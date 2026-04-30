import sys
import os

from _local_rem_card_bootstrap import bootstrap_local_rem_card


def _strip_legacy_jornal_role(argv):
    normalized = [argv[0]]
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--role" and i + 1 < len(argv) and str(argv[i + 1]).lower() == "jornal":
            i += 2
            continue
        if arg.startswith("--role=") and arg.split("=", 1)[1].lower() == "jornal":
            i += 1
            continue
        normalized.append(arg)
        i += 1
    return normalized

def run_rem_card():
    bootstrap_local_rem_card()

    from rem_card.app.main import main
    main()

if __name__ == "__main__":
    sys.argv = _strip_legacy_jornal_role(sys.argv)
    run_rem_card()
