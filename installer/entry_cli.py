# SPDX-License-Identifier: GPL-3.0-only
# The bundle's ostinato-cli.exe (see ostinato.launcher).
import multiprocessing

from ostinato.launcher import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
