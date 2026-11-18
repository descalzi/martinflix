# lib/engine.sh — compose orchestration for stackctl.
# Sourced, not executed. Requires lib/ui.sh.

GIT_ROOT="${STACKCTL_GIT_ROOT:-/mnt/martinflix/Data/git}"
HEALTH_TIMEOUT="${STACKCTL_HEALTH_TIMEOUT:-360}"  # chained depends_on can take a few minutes

# ---- docker access ----------------------------------------------------------
engine_detect_docker() {
    if docker info >/dev/null 2>&1; then
        DOCKER=(docker)
    elif sudo -n docker info >/dev/null 2>&1; then
        DOCKER=(sudo -n docker)
        DOCKER_VIA_SUDO=1
    else
        ui_error "cannot talk to the docker daemon (tried direct and sudo -n)"
        exit 1
    fi
}

# ---- stacks.conf ------------------------------------------------------------
# Populates: STACK_IDS, S_TITLE, S_FILE, S_DIR, S_PROJ, S_NEEDS, S_HEALTH, S_FLAGS
declare -a STACK_IDS=()
declare -A S_TITLE S_FILE S_DIR S_PROJ S_NEEDS S_HEALTH S_FLAGS S_ISGIT

# Host secrets live off the shared folder. First readable wins.
engine_env_file() {
    local f
    for f in "/etc/docker-stacks/${STACK_HOST}.env" \
             "$HOME/.config/docker-stacks/${STACK_HOST}.env"; do
        [[ -r "$f" ]] && { printf '%s' "$f"; return 0; }
    done
    return 1
}

engine_load_conf() {
    local conf="$1" line id title file needs health flags
    [[ -r "$conf" ]] || { ui_error "no stacks.conf at $conf"; exit 1; }
    while IFS='|' read -r id title file needs health flags; do
        id="$(engine_trim "$id")"
        [[ -z "$id" || "$id" == \#* ]] && continue
        title="$(engine_trim "$title")"; file="$(engine_trim "$file")"
        needs="$(engine_trim "$needs")"; health="$(engine_trim "$health")"
        flags="$(engine_trim "${flags:-}")"

        if [[ "$file" == git:* ]]; then
            local app="${file#git:}"
            S_DIR[$id]="$GIT_ROOT/$app"
            S_FILE[$id]="$GIT_ROOT/$app/docker-compose.yml"
            S_PROJ[$id]="$app"
            S_ISGIT[$id]=1
        else
            S_DIR[$id]="$HOSTDIR"
            S_FILE[$id]="$HOSTDIR/$file"
            S_PROJ[$id]="${STACK_HOST}-${id}"
            S_ISGIT[$id]=0
        fi
        S_TITLE[$id]="$title"; S_NEEDS[$id]="$needs"
        S_HEALTH[$id]="$health"; S_FLAGS[$id]="$flags"
        STACK_IDS+=("$id")
    done < "$conf"
}

engine_trim() { local s="$1"; s="${s#"${s%%[![:space:]]*}"}"; printf '%s' "${s%"${s##*[![:space:]]}"}"; }

engine_is_manual() { [[ ",${S_FLAGS[$1]}," == *,manual,* ]]; }

engine_default_stacks() {
    local id
    for id in "${STACK_IDS[@]}"; do engine_is_manual "$id" || printf '%s\n' "$id"; done
}

# ---- dependency resolution --------------------------------------------------
# Expands the requested ids to include prerequisites, in start order.
engine_resolve() {
    local -a want=("$@") out=()
    local -A seen=()
    _visit() {
        local id="$1" dep
        [[ -n "${seen[$id]:-}" ]] && return 0
        [[ -n "${S_TITLE[$id]:-}" ]] || { ui_error "unknown stack: $id — stacks on $STACK_HOST: ${STACK_IDS[*]}"; exit 1; }
        seen[$id]=1
        IFS=',' read -ra _deps <<< "${S_NEEDS[$id]}"
        for dep in "${_deps[@]}"; do
            dep="$(engine_trim "$dep")"; [[ -n "$dep" ]] && _visit "$dep"
        done
        out+=("$id")
    }
    local id; for id in "${want[@]}"; do _visit "$id"; done
    printf '%s\n' "${out[@]}"
}

# ---- compose plumbing -------------------------------------------------------
engine_compose() {
    local id="$1"; shift
    local -a ef=()
    # git: stacks keep their own .env next to the source; host stacks read the
    # host secrets file. Never both — --env-file replaces compose's default.
    if [[ "${S_ISGIT[$id]}" == "0" ]] && envf="$(engine_env_file)"; then
        ef=(--env-file "$envf")
    fi
    ( cd "${S_DIR[$id]}" && "${DOCKER[@]}" compose -p "${S_PROJ[$id]}" \
        "${ef[@]}" -f "${S_FILE[$id]}" "$@" )
}

engine_has_build() { grep -qE '^\s+build:' "${S_FILE[$1]}" 2>/dev/null; }

# Containers named in the file that already exist under a *different* compose
# project. Adopting them means removing the old container first.
engine_conflicts() {
    local id="$1" name proj
    grep -oP '^\s*container_name:\s*\K\S+' "${S_FILE[$id]}" 2>/dev/null | while read -r name; do
        proj=$("${DOCKER[@]}" inspect "$name" \
               --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>/dev/null) || continue
        [[ "$proj" == "${S_PROJ[$id]}" ]] || printf '%s\t%s\n' "$name" "${proj:-<docker run>}"
    done
}

# ---- health -----------------------------------------------------------------
engine_container_state() {
    "${DOCKER[@]}" inspect "$1" --format \
        '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null \
        || printf 'missing|none'
}

# engine_wait_health <id> -> 0 healthy, 1 timeout/unhealthy
engine_wait_health() {
    local id="$1" deadline=$(( SECONDS + HEALTH_TIMEOUT ))
    local -a names=()
    IFS=',' read -ra names <<< "${S_HEALTH[$id]}"
    [[ ${#names[@]} -eq 0 || -z "${names[0]}" ]] && return 0

    local n st status health pending i=0
    while (( SECONDS < deadline )); do
        pending=""
        for n in "${names[@]}"; do
            n="$(engine_trim "$n")"; [[ -z "$n" ]] && continue
            st="$(engine_container_state "$n")"
            status="${st%%|*}"; health="${st##*|}"
            case "$status:$health" in
                running:healthy|running:none) : ;;
                running:starting) pending="$pending $n" ;;
                running:unhealthy) ui_step_end fail "unhealthy: $n"; return 1 ;;
                *) pending="$pending $n" ;;
            esac
        done
        [[ -z "$pending" ]] && return 0
        ui_step_status "waiting on health:$pending" "${SPINNER:i++%${#SPINNER}:1}"
        sleep 1
    done
    ui_step_end fail "health timeout after ${HEALTH_TIMEOUT}s:$pending"
    return 1
}

# ---- container inventory ----------------------------------------------------
# The containers a stack SHOULD have. Read from `compose config`, which emits
# normalised YAML with ${VARS} and <<: merges resolved.
engine_stack_containers() {
    local id="$1"; local -a names=()
    mapfile -t names < <(engine_compose "$id" config 2>/dev/null \
                           | grep -oP '^\s*container_name:\s*\K\S+' | tr -d '"')
    [[ ${#names[@]} -eq 0 ]] && mapfile -t names < <(engine_compose "$id" ps -a --format '{{.Name}}' 2>/dev/null)
    printf '%s\n' "${names[@]}"
}

# engine_glyph_for <status-string> <state> <manual 0|1> -> "<glyph>\t<colour>\t<problem|>"
engine_glyph_for() {
    local st="$1" state="$2" manual="$3"
    case "$state" in
        running)
            if   [[ "$st" == *"(unhealthy)"* ]];      then printf '%s\t%s\tunhealthy\n' "$GLYPH_ERR" "$C_RED"
            elif [[ "$st" == *"health: starting"* ]]; then printf '%s▸%s\t%s\t\n' "$C_YEL" "$C_RST" "$C_YEL"
            else printf '%s\t%s\t\n' "$GLYPH_OK" "$C_GRN"; fi ;;
        restarting|paused) printf '%s!%s\t%s\t%s\n' "$C_YEL" "$C_RST" "$C_YEL" "$state" ;;
        *)  if (( manual )); then printf '%s\t%s\t\n' "$GLYPH_IDLE" "$C_DIM"
            else printf '%s\t%s\t%s\n' "$GLYPH_ERR" "$C_RED" "${state/missing/not created}"; fi ;;
    esac
}

# ---- rollback bookkeeping ---------------------------------------------------
engine_record_digests() {
    local id="$1" out="$STATE_DIR/${id}.digests" n
    : > "$out.tmp"
    engine_compose "$id" ps --format '{{.Name}}' 2>/dev/null | while read -r n; do
        [[ -z "$n" ]] && continue
        printf '%s\t%s\n' "$n" \
            "$("${DOCKER[@]}" inspect "$n" --format '{{.Image}}' 2>/dev/null)" >> "$out.tmp"
    done
    [[ -s "$out.tmp" ]] && mv "$out.tmp" "$out" || rm -f "$out.tmp"
}
