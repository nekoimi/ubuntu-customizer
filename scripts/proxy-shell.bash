[[ -n ${BASH_VERSION:-} ]] || return 0

proxy-on() {
    if (($#)); then echo 'Usage: proxy-on' >&2; return 2; fi
    export http_proxy=http://127.0.0.1:20171 https_proxy=http://127.0.0.1:20171
    export HTTP_PROXY=$http_proxy HTTPS_PROXY=$https_proxy
    export all_proxy=$http_proxy ALL_PROXY=$http_proxy
    export no_proxy='localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,.localhost,.local,.aliyun.com,.tsinghua.edu.cn'
    export NO_PROXY=$no_proxy
    echo 'Proxy enabled for this shell: http://127.0.0.1:20171'
    echo 'Connect a node in v2rayA first. Docker uses its own proxy configuration.'
}

proxy-off() {
    if (($#)); then echo 'Usage: proxy-off' >&2; return 2; fi
    unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY no_proxy NO_PROXY
    echo 'Proxy environment cleared for this shell. Docker configuration is unchanged.'
}

proxy-status() {
    if (($#)); then echo 'Usage: proxy-status' >&2; return 2; fi
    local variable value
    for variable in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY no_proxy NO_PROXY; do
        value=${!variable-}
        if [[ -z $value ]]; then
            printf '%s: unset or empty\n' "$variable"
        elif [[ $variable == no_proxy || $variable == NO_PROXY ]]; then
            printf '%s: set (bypass list)\n' "$variable"
        elif [[ $value == http://127.0.0.1:20171 ]]; then
            printf '%s: local v2rayA (127.0.0.1:20171)\n' "$variable"
        else
            printf '%s: custom value (hidden)\n' "$variable"
        fi
    done
    echo 'Shell environment only; sudo may discard it. Docker uses its own proxy configuration.'
}
