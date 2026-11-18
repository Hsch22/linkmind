#!/usr/bin/env bash
# LinkMind on this computer: macOS, or Linux -- including Ubuntu in WSL2 on a Windows PC. Background services start
# with the computer and restart within seconds if they stop (macOS: launchd LaunchAgents in ~/Library/LaunchAgents;
# Linux: systemd user units in ~/.config/systemd/user):
#   com.linkmind.postgres     database ($LINKMIND_HOME/pg, 127.0.0.1:5544)
#   com.linkmind.core         LinkMind Core MCP server (http://127.0.0.1:8765/mcp)
#   com.linkmind.core.<name>  one more Core per tester (own port, database linkmind_<name>, files)
#   OpenClaw Gateway + WeChat, installed by `openclaw gateway install` (ai.openclaw.gateway / openclaw-gateway)
# Runtime data lives in $LINKMIND_HOME (default ~/.linkmind), outside the repo. The Core services run an installed
# copy in $LINKMIND_HOME/app (refreshed by `up`): on macOS, launchd jobs may not open ~/Documents.
#   scripts/dev.sh install              # first time on a new computer: tools, model key, services, WeChat login
#   scripts/dev.sh up                   # install or update everything and (re)start Core; rerun after changes
#   scripts/dev.sh status
#   scripts/dev.sh logs                 # follow Core and gateway logs
#   scripts/dev.sh login                # WeChat QR login for your own bot (first time, or after it logged out)
#   scripts/dev.sh add-tester NAME      # someone else gets their own bot: they scan a QR code, data kept apart
#   scripts/dev.sh remove-tester NAME   # delete a tester's bot login, agent, database and files
#   scripts/dev.sh down                 # stop all services until the next login/boot or `up`
#   scripts/dev.sh uninstall            # stop and remove the services (data stays)
#   scripts/dev.sh sync                 # dev venv for tests and scripts ($LINKMIND_HOME/venv)
#   scripts/dev.sh smoke [args]         # scripts/mcp_smoke.py against the running MCP server
#   scripts/dev.sh eval DIR [args]      # scripts/eval_screenshots.py
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OS="$(uname -s)"  # Darwin or Linux
# Versions `install` sets up on a new computer (the ones tested on the main install).
OPENCLAW_VERSION=2026.11.2
WEIXIN_PLUGIN=@tencent-weixin/openclaw-weixin@2.4.10
# One conda env holds Python 3.15, PostgreSQL 18 + pgvector, Node 26 and uv: the existing one, else Miniforge's.
if [[ -z "${LINKMIND_CONDA_ENV:-}" ]]; then
  LINKMIND_CONDA_ENV="$HOME/miniforge3/envs/linkmind"
  [[ -d /opt/miniconda3/envs/linkmind ]] && LINKMIND_CONDA_ENV=/opt/miniconda3/envs/linkmind
fi
export LINKMIND_CONDA_ENV
CONDA_ENV="$LINKMIND_CONDA_ENV"
export LINKMIND_HOME="${LINKMIND_HOME:-$HOME/.linkmind}"
export PATH="$CONDA_ENV/bin:$PATH"
cmd="${1:-status}"
[[ $# -gt 0 ]] && shift
# The services connect directly: the WeChat iLink backend and the model gateway need no proxy. `install` keeps any
# proxy for its downloads.
[[ "$cmd" == install ]] || unset HTTPS_PROXY https_proxy HTTP_PROXY http_proxy
DEV_VENV="$LINKMIND_HOME/venv"
APP="$LINKMIND_HOME/app"
LOGS="$LINKMIND_HOME/logs"
PG_JOB=com.linkmind.postgres
CORE_JOB=com.linkmind.core
if [[ "$OS" == Darwin ]]; then
  JOBS="$HOME/Library/LaunchAgents"
  DOMAIN="gui/$(id -u)"
  GW_JOB=ai.openclaw.gateway
else
  JOBS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  GW_JOB=openclaw-gateway
fi
mkdir -p "$LOGS" "$JOBS"

tenants() { python3 "$ROOT/scripts/tenants.py" "$@"; }

# Background jobs: launchd LaunchAgents on macOS, systemd user units on Linux.
job_file() { if [[ "$OS" == Darwin ]]; then echo "$JOBS/$1.plist"; else echo "$JOBS/$1.service"; fi; }
loaded() { # running (macOS: loaded into launchd)
  if [[ "$OS" == Darwin ]]; then launchctl print "$DOMAIN/$1" >/dev/null 2>&1; else systemctl --user is-active --quiet "$1"; fi
}
not_loaded() { ! loaded "$1"; }
job_state() {
  if [[ "$OS" == Darwin ]]; then launchctl print "$DOMAIN/$1" | awk -F'= ' '/^\tstate/ {print $2; exit}'
  else systemctl --user is-active "$1" || true; fi
}
restart_job() { if [[ "$OS" == Darwin ]]; then launchctl kickstart -k "$DOMAIN/$1"; else systemctl --user restart "$1"; fi; }
stop_job() { # until the next login/boot or `up`
  if [[ "$OS" == Darwin ]]; then launchctl bootout "$DOMAIN/$1" 2>/dev/null || true; else systemctl --user stop "$1" 2>/dev/null || true; fi
}
remove_job() {
  if [[ "$OS" == Darwin ]]; then
    stop_job "$1"; rm -f "$(job_file "$1")"
  else
    systemctl --user disable --now "$1" >/dev/null 2>&1 || true
    rm -f "$(job_file "$1")"; systemctl --user daemon-reload
  fi
}

# write_job LABEL LOGFILE PROGRAM [ARGS...]: a job that starts with the computer and is restarted whenever it exits.
# Extra environment variables come from JOB_ENV ("KEY=VALUE" lines); RESTART=1 restarts an unchanged job.
JOB_ENV=""
write_job() {
  local label="$1" log="$2"; shift 2
  local file new a line args="" envs=""
  file="$(job_file "$label")"; new="$file.new"
  if [[ "$OS" == Darwin ]]; then
    for a in "$@"; do args+="    <string>$a</string>"$'\n'; done
    while IFS= read -r line; do
      [[ -n "$line" ]] && envs+="    <key>${line%%=*}</key><string>${line#*=}</string>"$'\n'
    done <<<"$JOB_ENV"
    cat >"$new" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
$args  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$CONDA_ENV/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>LINKMIND_HOME</key><string>$LINKMIND_HOME</string>
$envs  </dict>
  <key>WorkingDirectory</key><string>$LINKMIND_HOME</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>$log</string>
  <key>StandardErrorPath</key><string>$log</string>
</dict>
</plist>
EOF
  else
    for a in "$@"; do args+=" \"$a\""; done
    while IFS= read -r line; do [[ -n "$line" ]] && envs+="Environment=\"$line\""$'\n'; done <<<"$JOB_ENV"
    cat >"$new" <<EOF
[Unit]
Description=LinkMind $label

[Service]
ExecStart=${args# }
Environment="PATH=$CONDA_ENV/bin:/usr/local/bin:/usr/bin:/bin"
Environment="LINKMIND_HOME=$LINKMIND_HOME"
${envs}WorkingDirectory=$LINKMIND_HOME
Restart=always
RestartSec=5
StandardOutput=append:$log
StandardError=append:$log

[Install]
WantedBy=default.target
EOF
  fi
  if cmp -s "$new" "$file" && loaded "$label"; then
    rm "$new"
    [[ "${RESTART:-}" == 1 ]] && restart_job "$label"
    return 0
  fi
  if [[ "$OS" == Darwin ]]; then
    if loaded "$label"; then
      launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
      wait_for 30 not_loaded "$label" || true  # bootout returns before the job is gone
    fi
    mv "$new" "$file"
    launchctl bootstrap "$DOMAIN" "$file"
  else
    mv "$new" "$file"
    systemctl --user daemon-reload
    systemctl --user enable --quiet "$label"
    systemctl --user restart "$label"
  fi
  echo "started background service $label"
}

wait_for() { # wait_for SECONDS COMMAND...
  local n=$(( $1 * 4 )); shift
  for _ in $(seq 1 "$n"); do "$@" >/dev/null 2>&1 && return 0; sleep 0.25; done
  return 1
}
port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

check_name() {
  [[ "${1:-}" =~ ^[a-z0-9]{1,16}$ && "$1" != main && "$1" != guest ]] \
    || { echo "tester name: 1-16 lowercase letters or digits, e.g. alice"; exit 2; }
}

# start_core JOB PORT LOG: (re)start one Core service, on freshly installed code, with the settings in JOB_ENV.
start_core() {
  RESTART=1 write_job "$1" "$3" "$APP/bin/linkmind-mcp"
  wait_for 20 port_open "$2" || { echo "$1 did not come up, see $3"; exit 1; }
}

# The gateway picks up a newly logged-in bot only after a restart; wait until WeChat is connected.
restart_gateway() {
  openclaw gateway restart >/dev/null 2>&1 || restart_job "$GW_JOB"
  wait_for 60 sh -c "openclaw channels status 2>/dev/null | grep -q '$1.*running'" || true
}

# WSL stops a Linux distro soon after its last terminal closes, systemd services or not. Starting `dbus-launch true`
# through wsl.exe leaves a daemon behind that keeps it running (OpenClaw docs: platforms/windows.md, "Gateway
# auto-start"). A minimized shortcut in the Windows Startup folder does that at every Windows login.
win_powershell() { # run a PowerShell script on the Windows side
  (cd /mnt/c && /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -NoProfile -NonInteractive \
    -EncodedCommand "$(printf '%s' "$1" | iconv -f utf-8 -t utf-16le | base64 -w0)") >/dev/null
}
WIN_SHORTCUT="Join-Path ([Environment]::GetFolderPath('Startup')) 'LinkMind.lnk'"
wsl_autostart() {
  win_powershell "\$s = (New-Object -ComObject WScript.Shell).CreateShortcut(($WIN_SHORTCUT))
\$s.TargetPath = Join-Path \$env:SystemRoot 'System32\\wsl.exe'
\$s.Arguments = '-d $WSL_DISTRO_NAME --exec dbus-launch true'
\$s.WindowStyle = 7
\$s.Save()"
  (cd /mnt/c && /mnt/c/Windows/System32/wsl.exe -d "$WSL_DISTRO_NAME" --exec dbus-launch true) >/dev/null 2>&1 || true
}

case "$cmd" in
  install)
    # First-time setup on a new computer; rerunning it skips whatever is already done.
    if [[ "$OS" == Linux ]]; then
      if [[ ! -d /run/systemd/system ]]; then
        if [[ -n "${WSL_DISTRO_NAME:-}" ]]; then
          echo "先打开 WSL 的 systemd（会要你的 Ubuntu 密码）："
          printf '[boot]\nsystemd=true\n' | sudo tee -a /etc/wsl.conf >/dev/null
          echo "好了。请在 Windows 的 PowerShell 里运行 wsl --shutdown，重新打开 Ubuntu，再运行一次：$0 install"
        else
          echo "LinkMind needs systemd to run its background services on Linux."
        fi
        exit 1
      fi
      need=()
      [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" == yes ]] || need+=(linger)
      [[ -n "${WSL_DISTRO_NAME:-}" ]] && ! command -v dbus-launch >/dev/null && need+=(dbus)
      if [[ ${#need[@]} -gt 0 ]]; then
        echo "让 LinkMind 在关掉终端后继续运行（会要你的 Ubuntu 密码）："
        [[ " ${need[*]} " == *" linger "* ]] && sudo loginctl enable-linger "$USER"
        [[ " ${need[*]} " == *" dbus "* ]] && { sudo apt-get update -qq && sudo apt-get install -y -qq dbus-x11 >/dev/null; }
      fi
    fi
    if [[ ! -x "$CONDA_ENV/bin/postgres" ]]; then
      base="$HOME/miniforge3"
      if [[ ! -x "$base/bin/conda" ]]; then
        echo "下载 Miniforge（装 Python、数据库等用）..."
        inst="$(mktemp)"
        curl -fSL --progress-bar -o "$inst" \
          "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-$(uname -s | sed s/Darwin/MacOSX/)-$(uname -m).sh"
        bash "$inst" -b -p "$base" >/dev/null
        rm -f "$inst"
      fi
      echo "安装 Python 3.15、PostgreSQL 18、Node 26（几分钟）..."
      "$base/bin/conda" create -y -q -p "$CONDA_ENV" -c conda-forge --override-channels \
        "python=3.15" "postgresql=18" pgvector "nodejs=26" uv >/dev/null
    fi
    if [[ "$(openclaw --version 2>/dev/null)" != *"$OPENCLAW_VERSION"* ]]; then
      echo "安装 OpenClaw $OPENCLAW_VERSION ..."
      npm install -g --no-fund --no-audit --loglevel=error "openclaw@$OPENCLAW_VERSION" >/dev/null
    fi
    mkdir -p "$HOME/.openclaw" && chmod 700 "$HOME/.openclaw"
    if ! grep -qs '^VECTRON_API_KEY=.' "$HOME/.openclaw/.env"; then
      read -r -s -p "粘贴模型 API Key（Vectron；输入时不显示，粘贴后按回车）：" key; echo
      [[ -n "$key" ]] || { echo "没有输入 Key，已停止"; exit 1; }
      (umask 077; echo "VECTRON_API_KEY=$key" >>"$HOME/.openclaw/.env")
      unset key
    fi
    if [[ ! -f "$HOME/.openclaw/openclaw.json" ]]; then
      echo "初始化 OpenClaw ..."
      openclaw onboard --non-interactive --accept-risk --mode local --flow quickstart --auth-choice skip \
        --gateway-auth token --gateway-bind loopback --gateway-port 18789 --no-install-daemon \
        --skip-channels --skip-skills --skip-search --skip-hooks --skip-ui --skip-health \
        --suppress-gateway-token-output >/dev/null
    fi
    if ! openclaw plugins list 2>/dev/null | grep -q openclaw-weixin; then
      echo "安装微信插件 ..."
      openclaw plugins install "$WEIXIN_PLUGIN" >/dev/null
    fi
    [[ -f "$ROOT/.env" ]] || cp "$ROOT/.env.example" "$ROOT/.env"
    "$0" up
    if [[ -n "${WSL_DISTRO_NAME:-}" ]]; then
      if wsl_autostart; then echo "已设置：登录 Windows 后 LinkMind 自动在后台运行"
      else echo "没能设置开机自动运行：每次开机后打开一次 Ubuntu 窗口即可（打开后可以关掉）"; fi
    fi
    if [[ -z "$(tenants main-account)" ]]; then
      "$0" login
    fi
    echo
    echo "装好了。在微信里找到 ClawBot 就可以用；电脑开着、联网就能回复。"
    ;;
  up)
    # Database
    "$ROOT/scripts/pg.sh" init
    JOB_ENV="" write_job "$PG_JOB" "$LOGS/pg.log" "$CONDA_ENV/bin/postgres" -D "$LINKMIND_HOME/pg"
    wait_for 20 pg_isready -h 127.0.0.1 -p 5544 || { echo "Postgres did not come up, see $LOGS/pg.log"; exit 1; }
    "$ROOT/scripts/pg.sh" createdb
    "$ROOT/scripts/pg.sh" migrate
    # Core: install the current code (not editable) and settings into $APP, then (re)start every Core service.
    (cd "$ROOT/core" && UV_PROJECT_ENVIRONMENT="$APP" uv sync --quiet --frozen --no-dev --no-editable \
      --reinstall-package linkmind-core --python "$CONDA_ENV/bin/python3.15")
    if [[ -f "$ROOT/.env" ]]; then install -m 600 "$ROOT/.env" "$APP/.env"; else rm -f "$APP/.env"; fi
    JOB_ENV="LINKMIND_WECHAT_ACCOUNT=$(tenants main-account)"
    start_core "$CORE_JOB" 8765 "$LOGS/mcp.log"
    echo "Core up on http://127.0.0.1:8765/mcp"
    for t in $(tenants list); do
      port="$(tenants get "$t" PORT)"
      LINKMIND_DB="linkmind_$t" "$ROOT/scripts/pg.sh" createdb
      LINKMIND_DB="linkmind_$t" "$ROOT/scripts/pg.sh" migrate
      mkdir -p "$LINKMIND_HOME/tenants/$t/raw" "$HOME/.openclaw/workspace-$t"
      # Own database, port and bot; no owner names from .env; files only from their own agent's workspace.
      JOB_ENV="LINKMIND_TENANT=$t
LINKMIND_MCP_PORT=$port
LINKMIND_DATABASE_URL=postgresql://127.0.0.1:5544/linkmind_$t
LINKMIND_WECHAT_ACCOUNT=$(tenants get "$t" ACCOUNT)
LINKMIND_OWNER_NAMES=
LINKMIND_MEDIA_ROOTS=$HOME/.openclaw/workspace-$t,$LINKMIND_HOME/tenants/$t"
      start_core "$CORE_JOB.$t" "$port" "$LOGS/mcp-$t.log"
      echo "Core for tester $t up on http://127.0.0.1:$port/mcp"
    done
    # OpenClaw: agent instructions, tool policy, testers' agents/bindings/servers (hot-reloaded), gateway service
    mkdir -p "$HOME/.openclaw/workspace"
    install -m 600 "$ROOT/openclaw/AGENTS.md" "$HOME/.openclaw/workspace/AGENTS.md"
    for t in $(tenants list); do install -m 600 "$ROOT/openclaw/AGENTS.md" "$HOME/.openclaw/workspace-$t/AGENTS.md"; done
    openclaw config patch --file "$ROOT/openclaw/config.json5" >/dev/null 2>&1 \
      || { echo "failed to apply openclaw/config.json5; run: openclaw config patch --file openclaw/config.json5"; exit 1; }
    patch="$(mktemp)"
    tenants openclaw-patch >"$patch"
    openclaw config patch --file "$patch" --replace-path mcp.servers >/dev/null 2>&1 \
      || { echo "failed to apply the testers' OpenClaw settings:"; cat "$patch"; rm -f "$patch"; exit 1; }
    rm -f "$patch"
    echo "synced AGENTS.md and OpenClaw settings ($(tenants list | grep -c . || true) testers)"
    if ! loaded "$GW_JOB"; then
      openclaw gateway install --runtime-path "$CONDA_ENV/bin/node" >/dev/null
      echo "started background service $GW_JOB"
    fi
    ;;
  login)
    # Your own bot. A new login replaces the previous one (which is logged out), so exactly one bot is yours.
    before="$(mktemp)"
    tenants accounts >"$before"
    first="$([[ -z "$(tenants main-account)" ]] && echo 1 || true)"
    echo "用你自己的微信扫下面的二维码。每张约 2 分钟有效，过期会自动换新的，几次之后停止（重新运行即可）。"
    openclaw channels login --channel openclaw-weixin || true
    account="$(tenants new-account "$before")"
    rm -f "$before"
    if [[ -n "$account" ]]; then
      for old in $(tenants owner-accounts); do
        [[ "$old" == "$account" ]] || openclaw channels logout --channel openclaw-weixin --account "$old" >/dev/null 2>&1 || true
      done
    fi
    [[ -n "$(tenants main-account)" ]] || { echo "没有扫码成功。重新运行：$0 login"; exit 1; }
    "$0" up >/dev/null  # your Core learns its bot; the bot is routed to your agent
    restart_gateway "$(tenants main-account)"
    if [[ -n "$first" ]] && tenants welcome main; then echo "使用说明已经发到你的微信里。"; fi
    ;;
  add-tester)
    name="${1:-}"; check_name "$name"
    dir="$LINKMIND_HOME/tenants/$name"
    [[ -n "$(tenants get "$name" ACCOUNT 2>/dev/null)" ]] && { echo "tester $name already has a WeChat bot"; exit 1; }
    if [[ ! -f "$dir/tenant.env" ]]; then
      mkdir -p "$dir/raw"
      echo "PORT=$(tenants next-port)" >"$dir/tenant.env"
    fi
    # Their database, Core and agent exist before their bot does, so nothing they send can reach your data.
    "$0" up >/dev/null
    before="$(mktemp)"
    tenants accounts >"$before"
    echo
    echo "请 $name 用自己的微信扫下面的二维码（每张约 2 分钟有效）。不在身边可以截图发给对方，对方在另一块屏幕上打开再用手机扫。"
    openclaw channels login --channel openclaw-weixin || true
    account="$(tenants new-account "$before")"
    rm -f "$before"
    [[ -n "$account" ]] || { echo "没有检测到新的微信登录。重新运行：scripts/dev.sh add-tester $name"; exit 1; }
    echo "ACCOUNT=$account" >>"$dir/tenant.env"
    "$0" up >/dev/null
    restart_gateway "$account"
    echo
    echo "完成：$name 的微信里现在有自己的 ClawBot，资料和你的完全分开。"
    if tenants welcome "$name"; then
      echo "使用说明已经发到 $name 的微信里。"
    else
      echo "使用说明没能自动发出，请把下面这段转给 $name："
      echo
      tenants welcome-text "$name" | sed 's/^/  /'
    fi
    ;;
  remove-tester)
    name="${1:-}"; check_name "$name"
    dir="$LINKMIND_HOME/tenants/$name"
    [[ -f "$dir/tenant.env" ]] || { echo "no tester named $name"; exit 1; }
    read -r -p "永久删除测试者 $name 的数据库、文件、聊天记录和微信 Bot 登录？输入 $name 确认：" answer
    [[ "$answer" == "$name" ]] || { echo "已取消"; exit 1; }
    account="$(tenants get "$name" ACCOUNT)"
    remove_job "$CORE_JOB.$name"
    [[ -n "$account" ]] && { openclaw channels logout --channel openclaw-weixin --account "$account" || true; }
    openclaw agents delete "$name" --force >/dev/null 2>&1 || true
    psql -h 127.0.0.1 -p 5544 -d postgres -qc "DROP DATABASE IF EXISTS linkmind_$name"
    rm -rf "$dir" "$HOME/.openclaw/workspace-$name"
    rmdir "$HOME/.openclaw/agents/$name" 2>/dev/null || true  # `agents delete` leaves it empty
    "$0" up >/dev/null
    echo "removed tester $name"
    ;;
  down)
    for job in "$GW_JOB" $(for t in $(tenants list); do echo "$CORE_JOB.$t"; done) "$CORE_JOB" "$PG_JOB"; do
      loaded "$job" && stop_job "$job" && echo "stopped $job" || true
    done
    ;;
  uninstall)
    "$0" down
    openclaw gateway uninstall >/dev/null 2>&1 || true
    for job in $(for t in $(tenants list); do echo "$CORE_JOB.$t"; done) "$CORE_JOB" "$PG_JOB"; do remove_job "$job"; done
    [[ -n "${WSL_DISTRO_NAME:-}" ]] && { win_powershell "Remove-Item -ErrorAction SilentlyContinue ($WIN_SHORTCUT)" || true; }
    echo "removed background services (data in $LINKMIND_HOME is kept)"
    ;;
  status)
    for job in "$PG_JOB" "$CORE_JOB" $(for t in $(tenants list); do echo "$CORE_JOB.$t"; done) "$GW_JOB"; do
      if loaded "$job"; then echo "$job: $(job_state "$job")"
      elif [[ -f "$(job_file "$job")" ]]; then echo "$job: not running (run scripts/dev.sh up)"
      else echo "$job: not installed (run scripts/dev.sh up)"; fi
    done
    for t in $(tenants list); do
      acc="$(tenants get "$t" ACCOUNT)"
      echo "tester $t: port $(tenants get "$t" PORT), WeChat bot ${acc:-not logged in yet}"
    done
    openclaw channels status 2>/dev/null | grep -i weixin || true
    ;;
  logs)
    exec tail -n 20 -F "$LOGS/mcp.log" "/tmp/openclaw/openclaw-$(date +%F).log"
    ;;
  sync)
    (cd "$ROOT/core" && UV_PROJECT_ENVIRONMENT="$DEV_VENV" uv sync --python "$CONDA_ENV/bin/python3.15")
    ;;
  smoke)
    exec "$DEV_VENV/bin/python" "$ROOT/scripts/mcp_smoke.py" "$@"
    ;;
  eval)
    exec "$DEV_VENV/bin/python" "$ROOT/scripts/eval_screenshots.py" "$@"
    ;;
  *)
    echo "usage: $0 {install|up|status|logs|login|add-tester|remove-tester|down|uninstall|sync|smoke|eval}"; exit 2
    ;;
esac
