-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- Synthetic AP schema for the C# parser fixture. Not customer code.
CREATE TABLE dbo.ApInvHdr (
    ApCo      tinyint        NOT NULL,
    ApTrans   int            NOT NULL,
    Vendor    int            NOT NULL,
    ApRef     varchar(15)    NULL,
    GrossAmt  money          NOT NULL,
    Status    char(1)        NOT NULL DEFAULT 'E',
    CONSTRAINT PK_ApInvHdr PRIMARY KEY (ApCo, ApTrans),
    CONSTRAINT CK_ApInvHdr_Status CHECK (Status IN ('E', 'P', 'V'))
)
GO
CREATE TABLE dbo.ApInvLine (
    ApCo      tinyint        NOT NULL,
    ApTrans   int            NOT NULL,
    LineNum   int            NOT NULL,
    Amount    money          NOT NULL
)
GO
