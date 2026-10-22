import sys

if sys.argv[1:2] == ["hook"]:
    # Agent hooks run on the agent's hot path: dispatch before importing the CLI.
    from .hooks import main_hook

    main_hook(sys.argv[2:])
else:
    from .cli import main

    main()
