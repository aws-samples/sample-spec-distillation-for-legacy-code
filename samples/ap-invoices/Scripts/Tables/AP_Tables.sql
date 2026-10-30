-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
-- =============================================================================
-- AP_Tables.sql   AP invoice tables + lookups
-- rev 2004-03 RDM, 2009-11 KLT (APApprLim), 2013-06 PSV (APHold*, APVendComp)
-- =============================================================================
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO

-- -----------------------------------------------------------------------------
-- company parameters
-- -----------------------------------------------------------------------------
CREATE TABLE dbo.APCoParm (
    APCo              tinyint       NOT NULL,
    GLCo              tinyint       NOT NULL,
    ApprThreshold     numeric(12,2) NOT NULL CONSTRAINT dfAPCoParm_ApprThr DEFAULT (10000.00),
    DupDays           smallint      NOT NULL CONSTRAINT dfAPCoParm_DupDays DEFAULT (90),
    LienWvrThreshold  numeric(12,2) NOT NULL CONSTRAINT dfAPCoParm_LWThr DEFAULT (5000.00),
    DfltRetgPct       numeric(6,3)  NOT NULL CONSTRAINT dfAPCoParm_Retg DEFAULT (10.000),
    LastMthSubClsd    smalldatetime NULL,      -- AP subledger closed thru this month
    AllowFutureInv    char(1)       NOT NULL CONSTRAINT dfAPCoParm_Fut DEFAULT ('N'),
    MgrHoldDays       smallint      NULL,      -- unused, was auto-release of MGR holds
    CONSTRAINT pkAPCoParm PRIMARY KEY CLUSTERED (APCo)
)
GO

CREATE TABLE dbo.GLMthStat (
    GLCo     tinyint       NOT NULL,
    Mth      smalldatetime NOT NULL,
    Status   char(1)       NOT NULL,   -- O open, C closed
    ClsdBy   varchar(30)   NULL,
    ClsdDate datetime      NULL,
    CONSTRAINT pkGLMthStat PRIMARY KEY CLUSTERED (GLCo, Mth)
)
GO

-- -----------------------------------------------------------------------------
-- vendors / compliance
-- -----------------------------------------------------------------------------
CREATE TABLE dbo.APVend (
    VendGrp      tinyint      NOT NULL,
    Vendor       int          NOT NULL,
    Name         varchar(60)  NOT NULL,
    ActiveYN     char(1)      NOT NULL DEFAULT ('Y'),
    VendType     char(1)      NOT NULL DEFAULT ('S'),  -- S sub, M material, U utility, O other
    V1099YN      char(1)      NOT NULL DEFAULT ('N'),
    TIN          varchar(11)  NULL,
    DfltRetgPct  numeric(6,3) NULL,
    PayTerms     smallint     NULL,
    CONSTRAINT pkAPVend PRIMARY KEY CLUSTERED (VendGrp, Vendor)
)
GO

CREATE TABLE dbo.APVendComp (
    VendGrp     tinyint       NOT NULL,
    Vendor      int           NOT NULL,
    CompCode    varchar(4)    NOT NULL,   -- GL general liab, WC workers comp, AUTO, UMB
    ExpDate     smalldatetime NULL,
    VerifiedYN  char(1)       NOT NULL DEFAULT ('N'),
    CONSTRAINT pkAPVendComp PRIMARY KEY CLUSTERED (VendGrp, Vendor, CompCode)
)
GO

CREATE TABLE dbo.JCJob (
    JCCo       tinyint      NOT NULL,
    Job        varchar(10)  NOT NULL,
    Descr      varchar(60)  NULL,
    JobStatus  tinyint      NOT NULL DEFAULT (1),   -- 1 open, 2 complete, 3 closed
    ProjMgr    varchar(30)  NULL,
    CONSTRAINT pkJCJob PRIMARY KEY CLUSTERED (JCCo, Job)
)
GO

CREATE TABLE dbo.SecUser (
    UserId   varchar(30) NOT NULL,
    APRole   varchar(5)  NOT NULL,
    ActiveYN char(1)     NOT NULL DEFAULT ('Y'),
    CONSTRAINT pkSecUser PRIMARY KEY CLUSTERED (UserId)
)
GO

-- -----------------------------------------------------------------------------
-- approval limits by role
-- -----------------------------------------------------------------------------
CREATE TABLE dbo.APApprLim (
    APRole  varchar(5)    NOT NULL,
    MaxAmt  numeric(12,2) NOT NULL,
    CONSTRAINT pkAPApprLim PRIMARY KEY CLUSTERED (APRole)
)
GO
INSERT dbo.APApprLim (APRole, MaxAmt) VALUES ('APCLK', 0)
INSERT dbo.APApprLim (APRole, MaxAmt) VALUES ('PM',    25000.00)
INSERT dbo.APApprLim (APRole, MaxAmt) VALUES ('CTRL',  100000.00)
INSERT dbo.APApprLim (APRole, MaxAmt) VALUES ('CFO',   999999999.99)
GO

-- -----------------------------------------------------------------------------
-- invoice header
-- -----------------------------------------------------------------------------
CREATE TABLE dbo.APInvHdr (
    APCo         tinyint       NOT NULL,
    Mth          smalldatetime NOT NULL,
    APTrans      int           NOT NULL,
    UIMth        smalldatetime NOT NULL,
    VendGrp      tinyint       NOT NULL,
    Vendor       int           NOT NULL,
    APRef        varchar(15)   NOT NULL,
    InvDate      smalldatetime NOT NULL,
    DueDate      smalldatetime NULL,
    PayTerms     smallint      NULL,
    Descr        varchar(60)   NULL,
    Job          varchar(10)   NULL,
    GrossAmt     numeric(12,2) NOT NULL,
    TaxAmt       numeric(12,2) NOT NULL DEFAULT (0),
    RetgAmt      numeric(12,2) NOT NULL DEFAULT (0),
    RetgRelAmt   numeric(12,2) NOT NULL DEFAULT (0),
    PaidAmt      numeric(12,2) NOT NULL DEFAULT (0),
    Status       char(1)       NOT NULL DEFAULT ('O'),
    LienWvrRcvd  char(1)       NOT NULL DEFAULT ('N'),
    DupOvrdBy    varchar(30)   NULL,
    EntBy        varchar(30)   NOT NULL,
    EntDate      datetime      NOT NULL,
    ApprBy       varchar(30)   NULL,
    ApprDate     datetime      NULL,
    RejReason    varchar(255)  NULL,
    VoidReason   varchar(255)  NULL,
    VoidBy       varchar(30)   NULL,
    VoidDate     datetime      NULL,
    PostedYN     char(1)       NOT NULL DEFAULT ('N'),
    CONSTRAINT pkAPInvHdr PRIMARY KEY CLUSTERED (APCo, Mth, APTrans),
    CONSTRAINT ckAPInvHdr_Status CHECK (Status IN ('O','P','A','H','X','V'))
)
GO
CREATE NONCLUSTERED INDEX ixAPInvHdr_Vend ON dbo.APInvHdr (APCo, VendGrp, Vendor, APRef)
GO

-- -----------------------------------------------------------------------------
-- invoice lines
-- -----------------------------------------------------------------------------
CREATE TABLE dbo.APInvLine (
    APCo      tinyint       NOT NULL,
    Mth       smalldatetime NOT NULL,
    APTrans   int           NOT NULL,
    APLine    smallint      NOT NULL,
    LineType  tinyint       NOT NULL,     -- 1 job, 2 expense, 3 equip (retired)
    JCCo      tinyint       NULL,
    Job       varchar(10)   NULL,
    Phase     varchar(20)   NULL,
    GLAcct    varchar(20)   NULL,
    Amount    numeric(12,2) NOT NULL,     -- excludes tax
    TaxAmt    numeric(12,2) NOT NULL DEFAULT (0),
    RetgPct   numeric(6,3)  NOT NULL DEFAULT (0),
    RetgAmt   numeric(12,2) NOT NULL DEFAULT (0),
    Descr     varchar(60)   NULL,
    CONSTRAINT pkAPInvLine PRIMARY KEY CLUSTERED (APCo, Mth, APTrans, APLine)
)
GO

-- -----------------------------------------------------------------------------
-- holds
-- -----------------------------------------------------------------------------
CREATE TABLE dbo.APHoldCode (
    HoldCode  varchar(4)  NOT NULL,
    Descr     varchar(40) NOT NULL,
    RelRole   varchar(5)  NOT NULL,   -- role allowed to release (CTRL/CFO override except INS)
    AutoYN    char(1)     NOT NULL DEFAULT ('N'),
    CONSTRAINT pkAPHoldCode PRIMARY KEY CLUSTERED (HoldCode)
)
GO
INSERT dbo.APHoldCode (HoldCode, Descr, RelRole, AutoYN) VALUES ('LW',  'Lien waiver not received',     'APCLK', 'Y')
INSERT dbo.APHoldCode (HoldCode, Descr, RelRole, AutoYN) VALUES ('INS', 'Vendor insurance expired',     'RISK',  'Y')
INSERT dbo.APHoldCode (HoldCode, Descr, RelRole, AutoYN) VALUES ('DSP', 'Disputed - see notes',         'PM',    'N')
INSERT dbo.APHoldCode (HoldCode, Descr, RelRole, AutoYN) VALUES ('MGR', 'Management hold',              'CTRL',  'N')
INSERT dbo.APHoldCode (HoldCode, Descr, RelRole, AutoYN) VALUES ('RET', 'Retainage held - job open',    'CTRL',  'Y')
GO

CREATE TABLE dbo.APHold (
    APCo      tinyint       NOT NULL,
    Mth       smalldatetime NOT NULL,
    APTrans   int           NOT NULL,
    HoldCode  varchar(4)    NOT NULL,
    HoldDate  datetime      NOT NULL,
    HoldBy    varchar(30)   NOT NULL,
    RelDate   datetime      NULL,
    RelBy     varchar(30)   NULL,
    Notes     varchar(255)  NULL,
    CONSTRAINT pkAPHold PRIMARY KEY CLUSTERED (APCo, Mth, APTrans, HoldCode, HoldDate)
)
GO

-- -----------------------------------------------------------------------------
-- audit
-- -----------------------------------------------------------------------------
CREATE TABLE dbo.APInvAudit (
    AuditId    int IDENTITY(1,1) NOT NULL,
    APCo       tinyint       NOT NULL,
    Mth        smalldatetime NOT NULL,
    APTrans    int           NOT NULL,
    OldStatus  char(1)       NULL,
    NewStatus  char(1)       NULL,
    ChgBy      varchar(30)   NOT NULL,
    ChgDate    datetime      NOT NULL,
    CONSTRAINT pkAPInvAudit PRIMARY KEY CLUSTERED (AuditId)
)
GO
