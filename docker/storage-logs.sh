#!/bin/env bash

docker ps -qa | xargs -I{} sh -c 'echo "$(sudo du -m $(docker inspect --format "{{.LogPath}}" {}) | cut -f1) MB	$(docker inspect --format "{{.Name}}" {} | cut -c2-)"' | sort -rn
