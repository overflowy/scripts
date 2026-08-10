#!/bin/env bash

docker ps -qa | while read -r id; do
	name=$(docker inspect --format '{{.Name}}' "$id" | cut -c2-)
	docker inspect --format '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}} {{.Source}}{{"\n"}}{{end}}{{end}}' "$id" | while read -r vol src; do
		[ -n "$vol" ] && echo "$(sudo du -sm "$src" | cut -f1) MB	$name	$vol"
	done
done | sort -rn
