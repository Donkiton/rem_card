"""Single public RemCard entrypoint."""
import multiprocessing
import sys

# A frozen Windows worker relaunches this same executable with private
# multiprocessing arguments.  Let the stdlib/PyInstaller dispatcher consume
# them before application bootstrap imports or CLI parsing run.
if __name__ == "__main__":
    multiprocessing.freeze_support()

from _local_rem_card_bootstrap import bootstrap_local_rem_card

bootstrap_local_rem_card()

if __name__ == "__main__":
    if sys.argv[1:2] == ["--remcard-infographic-worker"]:
        from rem_card.services.analytics.infographic_worker import main as infographic_worker_main

        raise SystemExit(infographic_worker_main(sys.argv[2:]))

    from rem_card.app.unified_main import main

    main()
