import sys


from _local_rem_card_bootstrap import bootstrap_local_rem_card


PROJECT_ROOT = bootstrap_local_rem_card()

from rem_card.app.updater_main import main


if __name__ == "__main__":
    sys.exit(main())
