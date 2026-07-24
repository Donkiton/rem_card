import multiprocessing

from _local_rem_card_bootstrap import bootstrap_local_rem_card

PROJECT_ROOT = bootstrap_local_rem_card()

if __name__ == "__main__":
    multiprocessing.freeze_support()

from rem_card.app.main import main


if __name__ == "__main__":
    main(path_setup=True)
