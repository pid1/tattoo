{ pkgs, lib, config, inputs, ... }:

let
  setupCommands = [
    "install-deps"
  ];
in
{
  # Packages
  packages = with pkgs; [
    uv
    ruff
    sqlite
    git
  ];

  # Language support
  languages.python = {
    enable = true;
    package = pkgs.python314;
    uv.enable = true;
  };

  # The app is a src-layout package that runs with src on PYTHONPATH rather
  # than being installed (rally convention).
  env.PYTHONPATH = "${config.env.DEVENV_ROOT}/src";

  # Pre-commit hooks
  pre-commit.hooks = {
    ruff.enable = true;
    ruff-format.enable = true;
  };

  # Scripts
  scripts = {
    # Setup
    setup.exec = lib.concatStringsSep " && " setupCommands;

    # Interactive dev commands
    dev.exec = ''
      uv run uvicorn tattoo.main:app --reload --host 0.0.0.0 --port 8000
    '';

    # Background dev commands
    dev-start.exec = ''
      mkdir -p .devenv/logs .devenv/pids
      nohup uv run uvicorn tattoo.main:app --reload --host 0.0.0.0 --port 8000 > .devenv/logs/dev.log 2>&1 &
      echo $! > .devenv/pids/dev.pid
      echo "✓ Dev server started in background (PID: $!)"
      echo "  Logs: .devenv/logs/dev.log"
      echo "  Stop: dev-stop"
    '';
    dev-stop.exec = ''
      if [ -f .devenv/pids/dev.pid ]; then
        pid=$(cat .devenv/pids/dev.pid)
        if kill -0 $pid 2>/dev/null; then
          kill $pid && echo "✓ Stopped dev server (PID: $pid)"
        else
          echo "Dev server not running"
        fi
        rm -f .devenv/pids/dev.pid
      else
        echo "No dev server PID file found"
      fi
    '';
    dev-status.exec = ''
      echo "=== Dev Process Status ==="
      for name in dev; do
        pidfile=".devenv/pids/$name.pid"
        if [ -f "$pidfile" ]; then
          pid=$(cat "$pidfile")
          if kill -0 $pid 2>/dev/null; then
            echo "$name: Running (PID: $pid)"
          else
            echo "$name: Stopped (stale PID file)"
          fi
        else
          echo "$name: Not started"
        fi
      done
    '';
    dev-logs.exec = "tail -50 .devenv/logs/dev.log 2>/dev/null || echo 'No dev logs found'";

    # Quality commands
    lint.exec = "ruff check .";
    lint-fix.exec = "ruff check . --fix";
    format.exec = "ruff format .";
    test.exec = "uv run pytest";
    check.exec = "ruff check . && ruff format --check . && uv run pytest";
    install-deps.exec = "uv sync";

    # Data commands
    run-once.exec = "uv run python -m tattoo.pipeline";
    add-source.exec = ''uv run python -m tattoo.add_source "$@"'';
    backup.exec = "uv run python -m tattoo.backup";
  };

  enterShell = ''
    echo "🪖 tattoo Development Environment"
    echo ""
    echo "Python: $(python --version)"
    echo "uv: $(uv --version)"
    echo ""
    echo "Setup:"
    echo "  setup            - Initialize repo (runs: ${lib.concatStringsSep ", " setupCommands})"
    echo ""
    echo "Interactive commands (block until killed):"
    echo "  dev              - Start FastAPI dev server (port 8000)"
    echo ""
    echo "Background commands (for agents/scripts):"
    echo "  dev-start        - Start dev server in background"
    echo "  dev-stop         - Stop background dev server"
    echo "  dev-status       - Check process status"
    echo "  dev-logs         - View recent logs"
    echo ""
    echo "Quality commands:"
    echo "  lint             - Run ruff linter"
    echo "  lint-fix         - Run ruff with auto-fix"
    echo "  format           - Run ruff formatter"
    echo "  test             - Run pytest"
    echo "  check            - lint + format check + tests (run before pushing)"
    echo ""
    echo "Data commands:"
    echo "  run-once         - Init the db and run the pipeline once"
    echo "  add-source       - Add a source row (type, feed_url, name)"
    echo "  backup           - Snapshot the database into <db-dir>/backups"
    echo ""
    echo "Other commands:"
    echo "  install-deps     - Install dependencies with uv"
    echo ""
  '';
}
