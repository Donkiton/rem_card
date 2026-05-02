from _local_rem_card_bootstrap import bootstrap_local_rem_card


def run_rem_card():
    bootstrap_local_rem_card()

    from rem_card.app.main import main
    main()

if __name__ == "__main__":
    run_rem_card()
