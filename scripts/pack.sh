#!/bin/bash
# 发版打包：把"该传的代码"整备到 dist/ 一个目录，上传只传 dist/，不用挑文件
cd "$(dirname "$0")/.." || exit 1
rm -rf dist
rsync -a --exclude dist --exclude var --exclude __pycache__ --exclude '*.pyc' \
      --exclude .DS_Store --exclude .zcode --exclude .git ./ dist/
echo "dist/ 就绪: $(du -sh dist | cut -f1)，条目 $(find dist -type f | wc -l | tr -d ' ')"
