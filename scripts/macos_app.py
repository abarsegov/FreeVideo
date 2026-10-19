"""Native Apple Silicon application; the interface is shared with Windows."""
import freevideo_source_bootstrap
freevideo_source_bootstrap.activate()

from freevideo_engine.modern_launcher import main

if __name__ == '__main__':
    main()
