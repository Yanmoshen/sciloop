# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
# 前端镜像：node 构建 → nginx 静态托管（同源代理 /api 到 backend，无跨域）

FROM node:18-alpine AS build
WORKDIR /app

ARG VITE_API_BASE=/api/v1
ARG VITE_DEFAULT_THEME=light
ENV VITE_API_BASE=$VITE_API_BASE \
    VITE_DEFAULT_THEME=$VITE_DEFAULT_THEME

# 一并复制 lockfile：只拷 package.json 会让 `npm install` 每次重新求解版本区间，
# 同一份源码在不同时间构建出不同产物（README-工程 §9 第 2 条「构建复现性不足」）。
COPY package.json package-lock.json ./
RUN npm install --no-audit --no-fund

COPY . .
# 生产构建必须覆盖 TypeScript 类型检查（主计划 P1-1）：`npm run build` = vue-tsc --noEmit && vite build。
# 类型错误将直接阻断镜像构建，不允许长期用 build:only 绕过类型检查。
RUN npm run build

FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
