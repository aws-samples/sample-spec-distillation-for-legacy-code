-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
-- =============================================================================
-- Report 412  AP Open Payables by Job (with retainage and holds)
--   Command source for "AP Open Payables by Job.rpt"
--   params: @APCo, @AsOfDate, @Job (NULL = all), @IncludeHeld ('Y'/'N')
--   KLT 2010-04, PSV 2013-06 holds column, PSV 2017-04 retainage released
-- =============================================================================
DECLARE @APCo tinyint, @AsOfDate smalldatetime, @Job varchar(10), @IncludeHeld char(1)
SELECT @APCo = {?APCo}, @AsOfDate = {?AsOfDate}, @Job = {?Job}, @IncludeHeld = {?IncludeHeld}

SELECT  h.APCo,
        ISNULL(h.Job, '(none)')                         AS Job,
        j.Descr                                          AS JobDescr,
        j.JobStatus,
        h.Mth, h.APTrans,
        h.VendGrp, h.Vendor, v.Name                      AS VendName,
        h.APRef, h.InvDate, h.DueDate,
        h.Status,
        h.GrossAmt,
        h.RetgAmt,
        h.RetgRelAmt,
        h.PaidAmt,
        -- open payable = gross less retainage still held less what's been paid
        h.GrossAmt - (h.RetgAmt - h.RetgRelAmt) - h.PaidAmt          AS OpenPayable,
        h.RetgAmt - h.RetgRelAmt                                       AS RetgHeld,
        DATEDIFF(day, h.DueDate, @AsOfDate)                            AS DaysPastDue,
        CASE WHEN DATEDIFF(day, h.DueDate, @AsOfDate) <= 0  THEN 'Current'
             WHEN DATEDIFF(day, h.DueDate, @AsOfDate) <= 30 THEN '1-30'
             WHEN DATEDIFF(day, h.DueDate, @AsOfDate) <= 60 THEN '31-60'
             WHEN DATEDIFF(day, h.DueDate, @AsOfDate) <= 90 THEN '61-90'
             ELSE 'Over 90' END                                        AS AgeBucket,
        holds.HoldList,
        CASE WHEN holds.BlockingCnt > 0 THEN 'Y' ELSE 'N' END          AS PayBlocked
FROM    dbo.APInvHdr h
JOIN    dbo.APVend v  ON v.VendGrp = h.VendGrp AND v.Vendor = h.Vendor
LEFT JOIN dbo.JCJob j ON j.JCCo = h.APCo AND j.Job = h.Job
OUTER APPLY (
        SELECT  HoldList = STUFF((SELECT ',' + x.HoldCode
                                    FROM dbo.APHold x
                                   WHERE x.APCo = h.APCo AND x.Mth = h.Mth AND x.APTrans = h.APTrans
                                     AND x.RelDate IS NULL
                                   ORDER BY x.HoldCode
                                     FOR XML PATH('')), 1, 1, ''),
                BlockingCnt = (SELECT COUNT(*) FROM dbo.APHold y
                                WHERE y.APCo = h.APCo AND y.Mth = h.Mth AND y.APTrans = h.APTrans
                                  AND y.RelDate IS NULL AND y.HoldCode <> 'RET')
        ) holds
WHERE   h.APCo = @APCo
  AND   h.Status IN ('A', 'H', 'P')          -- open payables only; O not posted, X paid, V void
  AND   (@Job IS NULL OR h.Job = @Job)
  AND   (@IncludeHeld = 'Y' OR h.Status <> 'H')
  AND   h.InvDate <= @AsOfDate
ORDER BY ISNULL(h.Job, '(none)'), v.Name, h.InvDate
