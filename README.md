# docker-codex

Запускать контейнер нужно либо с `--privileged`, либо как минимум с `--cap-add SYS_ADMIN --security-opt seccomp=unconfined` — иначе `bwrap` упадёт с `setting up uid map: Permission denied или clone3: Operation not permitted`. Default Docker seccomp/AppArmor блокирует нужные syscalls.