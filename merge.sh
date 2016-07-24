#!/bin/sh
#git remote add upstream git@github.com:
git fetch upstream
git checkout master
git merge upstream/master -m "-"

