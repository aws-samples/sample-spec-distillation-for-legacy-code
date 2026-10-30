# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
# Build context: runtimes/facts/structure (ported from the lean pipeline, eval/lean/structure)
# Run: tar -C <corpus> -cf - . | docker run --rm -i specdistill-lean-structure > facts.json
# (The corpus goes in as a tar stream on stdin, so no bind mount is needed.)
FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /build
COPY LeanStructure.csproj .
RUN dotnet restore
COPY Program.cs .
RUN dotnet publish -c Release -o /app --no-restore

FROM mcr.microsoft.com/dotnet/sdk:8.0
COPY --from=build /app /app
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /src && chown app:app /src
# Run as the image's built-in non-root user.
USER app
ENTRYPOINT ["/entrypoint.sh"]
