#!/bin/sh
#git remote add upstream git@github.com:rg3/youtube-dl.git
git fetch upstream
git checkout master
git merge upstream/master -m "-"

