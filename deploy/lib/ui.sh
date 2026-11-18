# lib/ui.sh — menus and progress rendering for stackctl.
# Sourced, not executed.

# ---- colour -----------------------------------------------------------------
if [[ -t 1 ]] && [[ "${NO_COLOR:-}" == "" ]]; then
    C_RST=$'\e[0m'; C_DIM=$'\e[2m';  C_B=$'\e[1m'
    C_GRN=$'\e[32m'; C_RED=$'\e[31m'; C_YEL=$'\e[33m'
    C_BLU=$'\e[34m'; C_CYN=$'\e[36m'
else
    C_RST=; C_DIM=; C_B=; C_GRN=; C_RED=; C_YEL=; C_BLU=; C_CYN=
fi

GLYPH_OK="${C_GRN}✔${C_RST}"
GLYPH_ERR="${C_RED}✘${C_RST}"
GLYPH_SKIP="${C_DIM}⊘${C_RST}"
GLYPH_RUN="${C_CYN}▸${C_RST}"
GLYPH_IDLE="${C_DIM}·${C_RST}"
SPINNER='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'

ui_header() {
    printf '\n%s%sstackctl%s %s·%s %s %s·%s %s\n\n' \
        "$C_B" "$C_CYN" "$C_RST" "$C_DIM" "$C_RST" "$1" "$C_DIM" "$C_RST" "$2"
}

ui_info()  { printf '  %s%s%s\n' "$C_DIM" "$1" "$C_RST"; }
ui_warn()  { printf '  %s! %s%s\n' "$C_YEL" "$1" "$C_RST"; }
ui_error() { printf '  %s%s %s%s\n' "$C_RED" "✘" "$1" "$C_RST" >&2; }

# ui_step_begin <id>  — opens an in-place line for a stack
ui_step_begin() { UI_ID="$1"; UI_T0=$SECONDS; printf '  %s %-10s %s' "$GLYPH_RUN" "$UI_ID" "starting…"; }

# ui_step_status <text> [spinner-char] — rewrite the open line
ui_step_status() {
    [[ -t 1 ]] || return 0
    printf '\r\033[2K  %s %-10s %s%s%s' "$GLYPH_RUN" "$UI_ID" "$1" "${2:+ }" "${2:-}"
}

# ui_step_end <ok|fail|skip> <text>
ui_step_end() {
    local el=$(( SECONDS - UI_T0 )) g
    case "$1" in
        ok)   g="$GLYPH_OK" ;;
        fail) g="$GLYPH_ERR" ;;
        *)    g="$GLYPH_SKIP" ;;
    esac
    if [[ -t 1 ]]; then printf '\r\033[2K'; else printf '\n'; fi
    printf '  %s %-10s %-44s %s%3ds%s\n' "$g" "$UI_ID" "$2" "$C_DIM" "$el" "$C_RST"
}

# ui_substep <glyph> <name> <text> — indented detail under a finished stack
ui_substep() { printf '      %s %-12s %s%s%s\n' "$1" "$2" "$C_DIM" "$3" "$C_RST"; }

# ui_run <label> <logfile> <cmd...> — run with spinner, return cmd's exit code
ui_run() {
    local label="$1" logf="$2"; shift 2
    local rc=0 i=0 pid
    "$@" >>"$logf" 2>&1 &
    pid=$!
    while kill -0 "$pid" 2>/dev/null; do
        ui_step_status "$label" "${SPINNER:i++%${#SPINNER}:1}"
        sleep 0.12
    done
    wait "$pid" || rc=$?
    return $rc
}

# ui_tail_log <logfile> [n] — show why a step failed.
# Docker build output uses bare \r for progress, so normalise line endings first.
# Prefer the actual error lines (which can be hundreds of lines above the end)
# over a raw tail, which for builds is mostly Dockerfile context.
ui_tail_log() {
    local n="${2:-12}" errs
    errs=$(tr '\r' '\n' < "$1" 2>/dev/null \
        | sed -E 's/^#[0-9]+ +//; s/^[0-9]+\.[0-9]+ +//' \
        | grep -E '(^|[[:space:]])(Error|ERROR|error|fatal|FATAL)[: ]|ERR_[A-Z_]+|╰─▶|help:|denied|not found|no space' \
        | grep -vE '^[[:space:]]*$' | awk '!seen[$0]++' | tail -n 6)
    if [[ -n "$errs" ]]; then
        printf '      %s┌─ errors ─%s\n' "$C_DIM" "$C_RST"
        printf '%s\n' "$errs" | sed "s/^/      ${C_DIM}│${C_RST} /"
    else
        printf '      %s┌─ last %s lines ─%s\n' "$C_DIM" "$n" "$C_RST"
        tr '\r' '\n' < "$1" 2>/dev/null | grep -vE '^[[:space:]]*$' | tail -n "$n" \
            | sed "s/^/      ${C_DIM}│${C_RST} /"
    fi
    printf '      %s└─ %s%s\n' "$C_DIM" "$1" "$C_RST"
}

# ---- inline selection -------------------------------------------------------
# No dialog framework. Plain ANSI, drawn in place, left in the scrollback when
# done — same visual language as the progress renderer above.
#
# Rendering and key reads go to /dev/tty, so these still work inside $( ).

_UI_TTY=/dev/tty
# -r/-w on /dev/tty succeed even with no controlling terminal; only opening it
# tells the truth. Probed once, then cached.
ui_have_tty() {
    if [[ -z "${_UI_TTY_OK:-}" ]]; then
        if { true >$_UI_TTY; } 2>/dev/null && { true <$_UI_TTY; } 2>/dev/null
        then _UI_TTY_OK=1; else _UI_TTY_OK=0; fi
    fi
    [[ "$_UI_TTY_OK" == 1 ]]
}
_ui_out() { printf "$@" > $_UI_TTY; }

_ui_key() {
    local k rest
    IFS= read -rsn1 k < $_UI_TTY 2>/dev/null || return 1
    case "$k" in
        '')    printf 'enter' ;;
        $'\t') printf 'next' ;;
        ' ')   printf 'space' ;;
        $'\e')
            IFS= read -rsn2 -t 0.05 rest < $_UI_TTY 2>/dev/null || rest=''
            case "$rest" in
                '[A') printf 'prev' ;;
                '[B') printf 'next' ;;
                '[Z') printf 'prev' ;;   # shift-tab
                *)    printf 'quit' ;;   # bare esc
            esac ;;
        j) printf 'next' ;;
        k) printf 'prev' ;;
        a) printf 'all'  ;;
        n) printf 'none' ;;
        q) printf 'quit' ;;
        *) printf 'other' ;;
    esac
}

_ui_cursor_hide() { ui_have_tty && _ui_out '\e[?25l'; }
_ui_cursor_show() { ui_have_tty && _ui_out '\e[?25h'; }

# ui_pick_stacks <id:title:ON|OFF>...  -> chosen ids on stdout, one per line
ui_pick_stacks() {
    local -a ids=() titles=() on=() on0=()
    local spec id title def
    for spec in "$@"; do
        id="${spec%%:*}"; spec="${spec#*:}"
        title="${spec%:*}"; def="${spec##*:}"
        ids+=("$id"); titles+=("$title")
        # OFF == flagged `manual` in stacks.conf. `a` restores this default set,
        # so "all" means the same thing here as `--all` does on the CLI.
        if [[ "$def" == ON ]]; then on+=(1); on0+=(1); else on+=(0); on0+=(0); fi
    done

    if ! ui_have_tty; then ui_pick_typed "$@"; return; fi

    local n=${#ids[@]} cur=0 i key
    _ui_cursor_hide
    trap '_ui_cursor_show' RETURN INT TERM

    _ui_out '\n  %sselect stacks%s  %sspace%s toggle · %stab/↑↓%s move · %sa%s all · %sn%s none · %s⏎%s run · %sq%s quit\n\n' \
        "$C_B" "$C_RST" "$C_CYN" "$C_DIM" "$C_CYN" "$C_DIM" "$C_CYN" "$C_DIM" \
        "$C_CYN" "$C_DIM" "$C_CYN" "$C_DIM" "$C_CYN" "$C_RST"

    while :; do
        for i in $(seq 0 $((n-1))); do
            local mark ptr tag
            (( on[i] )) && mark="${C_GRN}x${C_RST}" || mark=" "
            (( i == cur )) && ptr="${C_CYN}❯${C_RST}" || ptr=" "
            (( on0[i] )) && tag="" || tag=" ${C_YEL}(manual)${C_RST}"
            _ui_out '  %s [%s] %-10s %s%s%s%s\e[K\n' \
                "$ptr" "$mark" "${ids[i]}" "$C_DIM" "${titles[i]}" "$C_RST" "$tag"
        done
        key="$(_ui_key)" || key=quit
        case "$key" in
            next)  cur=$(( (cur + 1) % n )) ;;
            prev)  cur=$(( (cur - 1 + n) % n )) ;;
            space) on[cur]=$(( 1 - on[cur] )) ;;   # not (( )): a 0 result exits under set -e
            all)   for i in $(seq 0 $((n-1))); do on[i]=${on0[i]}; done ;;
            none)  for i in $(seq 0 $((n-1))); do on[i]=0; done ;;
            enter) break ;;
            quit)  _ui_out '\e[%dA\e[J' "$n"; _ui_cursor_show; return 1 ;;
        esac
        _ui_out '\e[%dA' "$n"
    done

    _ui_out '\e[%dA\e[J' "$n"
    _ui_cursor_show
    local -a chosen=()
    for i in $(seq 0 $((n-1))); do (( on[i] )) && chosen+=("${ids[i]}"); done
    (( ${#chosen[@]} )) && printf '%s\n' "${chosen[@]}"
}

# ui_pick_action -> action id on stdout
ui_pick_action() {
    local -a keys=(update up restart stop status logs dryrun)
    local -a desc=(
        "pull new images, then recreate"
        "start / apply config, no pull"
        "restart containers in place"
        "stop the stack"
        "show state, change nothing"
        "follow logs"
        "print what update would do"
    )
    if ! ui_have_tty; then
        printf 'action [%s] (update): ' "${keys[*]}" >&2
        local a; read -r a; printf '%s' "${a:-update}"; return
    fi

    local n=${#keys[@]} cur=0 i key
    _ui_cursor_hide
    trap '_ui_cursor_show' RETURN INT TERM
    _ui_out '\n  %saction%s  %stab/↑↓%s move · %s⏎%s select · %sq%s quit\n\n' \
        "$C_B" "$C_RST" "$C_CYN" "$C_DIM" "$C_CYN" "$C_DIM" "$C_CYN" "$C_RST"

    while :; do
        for i in $(seq 0 $((n-1))); do
            if (( i == cur )); then
                _ui_out '  %s❯%s %s%-8s%s %s%s%s\e[K\n' "$C_CYN" "$C_RST" \
                    "$C_B" "${keys[i]}" "$C_RST" "$C_DIM" "${desc[i]}" "$C_RST"
            else
                _ui_out '    %-8s %s%s%s\e[K\n' "${keys[i]}" "$C_DIM" "${desc[i]}" "$C_RST"
            fi
        done
        key="$(_ui_key)" || key=quit
        case "$key" in
            next) cur=$(( (cur + 1) % n )) ;;
            prev) cur=$(( (cur - 1 + n) % n )) ;;
            enter) break ;;
            quit) _ui_out '\e[%dA\e[J' "$n"; _ui_cursor_show; return 1 ;;
        esac
        _ui_out '\e[%dA' "$n"
    done
    _ui_out '\e[%dA\e[J' "$n"
    _ui_cursor_show
    printf '%s' "${keys[cur]}"
}

# Non-tty / piped: type numbers instead. "all" means the default set — the same
# stacks `--all` would pick — so `manual` ones must be named by number.
ui_pick_typed() {
    local -a ids=() defs=() sel=()
    local spec id def i reply
    for spec in "$@"; do
        id="${spec%%:*}"; def="${spec##*:}"
        ids+=("$id"); [[ "$def" == ON ]] && defs+=(1) || defs+=(0)
    done
    printf '\nstacks on %s:\n' "$STACK_HOST" >&2
    for i in "${!ids[@]}"; do
        (( defs[i] )) && printf '  %2d) %s\n' "$((i+1))" "${ids[i]}" >&2 \
                      || printf '  %2d) %s (manual)\n' "$((i+1))" "${ids[i]}" >&2
    done
    printf 'numbers, or "all": ' >&2
    read -r reply || reply=all
    if [[ "$reply" == "all" || -z "$reply" ]]; then
        for i in "${!ids[@]}"; do (( defs[i] )) && sel+=("${ids[i]}"); done
    else
        for i in $reply; do [[ -n "${ids[$((i-1))]:-}" ]] && sel+=("${ids[$((i-1))]}"); done
    fi
    (( ${#sel[@]} )) && printf '%s\n' "${sel[@]}"
}

ui_confirm() {
    local r
    if ui_have_tty; then
        printf '  %s [y/N] ' "$1" > $_UI_TTY
        IFS= read -r r < $_UI_TTY
    else
        printf '  %s [y/N] ' "$1" >&2; read -r r
    fi
    [[ "$r" =~ ^[Yy] ]]
}
