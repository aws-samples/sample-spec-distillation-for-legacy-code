// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
const path = require('path');

export function formatTotal(n) {
  return helper(n).toFixed(2);
}

const helper = (n) => Number(n);
