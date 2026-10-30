#!/bin/sh
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
# Unpack the corpus tar from stdin into /src, then emit facts.json on stdout (diagnostics on stderr).
set -e
mkdir -p /src
tar -xf - -C /src
exec dotnet /app/LeanStructure.dll /src
