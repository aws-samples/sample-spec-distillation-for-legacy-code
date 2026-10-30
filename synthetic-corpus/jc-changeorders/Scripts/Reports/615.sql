-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
-- =============================================================================
-- Report 615  JC Approved Change Order Log
--   command SQL for "JC Approved Change Order Log.rpt"
--   approved + posted change orders by job for an approval date range
--   params  @JCCo, @Job ('' = all), @FromDate, @ToDate, @COType ('' = all)
--   MJR 2010-04, TKB 2016-02 added source / exec flag
-- =============================================================================
SELECT  h.JCCo,
        h.Job,
        j.Description                       AS JobDescr,
        j.ContractType,
        j.ProjMgr,
        h.CONum,
        h.Description,
        h.COType,
        h.ReasonCode,
        r.Description                       AS ReasonDescr,
        h.Status,
        h.SL,
        h.ReqDate,
        h.ApprBy,
        h.ApprDate,
        h.CostAmt,
        h.MarkupPct,
        h.MarkupAmt,
        h.TaxAmt,
        h.TotalAmt,
        h.DaysExt,
        h.SourceSys,
        CASE WHEN h.TotalAmt > 50000 THEN 'Y' ELSE 'N' END AS ExecReq,
        CASE WHEN h.ApprBy = 'SYSTEM' THEN 'Y' ELSE 'N' END AS AutoAppr,
        j.OrigContract,
        j.RevContract
  FROM  dbo.JCCOHdr h
  JOIN  dbo.JCJob j       ON j.JCCo = h.JCCo AND j.Job = h.Job
  LEFT JOIN dbo.JCCOReason r ON r.ReasonCode = h.ReasonCode
 WHERE  h.JCCo = {?JCCo}
   AND  ({?Job} = '' OR h.Job = {?Job})
   AND  ({?COType} = '' OR h.COType = {?COType})
   AND  h.Status IN ('A', 'I')
   AND  h.ApprDate BETWEEN {?FromDate} AND {?ToDate}
 ORDER BY h.Job, h.CONum
