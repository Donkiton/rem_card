"""Single public RemCard entrypoint."""
import multiprocessing

# A frozen Windows worker relaunches this same executable with private
# multiprocessing arguments.  Let the stdlib/PyInstaller dispatcher consume
# them before application bootstrap imports or CLI parsing run.
if __name__ == "__main__":
    multiprocessing.freeze_support()

from _local_rem_card_bootstrap import bootstrap_local_rem_card

bootstrap_local_rem_card()

if __name__ == "__main__":
    from rem_card.app.unified_main import main

    main()
