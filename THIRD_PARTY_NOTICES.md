# Third-party notices

Home Energy Manager's own source code is licensed under the MIT License.

The application installs the following direct Python dependencies at image build time. Their
source code is not copied into this Git repository, but the resulting container image includes
them and their transitive dependencies.

## aiohttp 3.12.15

- Project: aio-libs/aiohttp
- License: Apache License 2.0
- Source: https://github.com/aio-libs/aiohttp

## paho-mqtt 2.1.0

- Project: Eclipse Paho MQTT Python Client
- License: Eclipse Public License 2.0 OR Eclipse Distribution License 1.0 (BSD-3-Clause)
- Source: https://github.com/eclipse-paho/paho.mqtt.python

Home Energy Manager uses Paho MQTT under its permissive Eclipse Distribution License 1.0
(BSD-3-Clause) option.

## Base container

The application Dockerfile uses the Docker Official Image packaging for Python (`python:3.13-alpine`).
The image combines software from multiple upstream projects, each under its respective license.
Their notices and package metadata are included in the built image as supplied by the upstream
image/package distributions.

This file is informational and does not replace the license texts shipped by the dependencies.
