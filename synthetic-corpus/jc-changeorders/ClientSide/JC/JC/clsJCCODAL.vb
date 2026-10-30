' Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
'==============================================================================
' clsJCCODAL.vb
' Data access for change orders, items, subcontracts.
' Column names are the db ones - see Scripts\Tables\JC_Tables.sql
'   JCCo = JC company   Job = job #   CONum = CO # within job ("001")
'   SL   = subcontract #  CostAmt/MarkupAmt/TaxAmt/TotalAmt are stored by
'   usp_JCCOSubmit - don't trust screen values
'==============================================================================
Option Strict Off
Option Explicit On

Imports System.Data
Imports System.Data.SqlClient
Imports System.Collections.Generic

Public Class clsJCCODAL

    Private m_LastErr As String = ""

    Public ReadOnly Property LastError() As String
        Get
            Return m_LastErr
        End Get
    End Property

    Private Function FillTable(ByVal sql As String, ByVal ParamArray prms() As SqlParameter) As DataTable
        Dim dt As New DataTable
        Using cn As SqlConnection = GetConn()
            Using cmd As New SqlCommand(sql, cn)
                For Each p As SqlParameter In prms
                    cmd.Parameters.Add(p)
                Next
                Dim da As New SqlDataAdapter(cmd)
                da.Fill(dt)
            End Using
        End Using
        Return dt
    End Function

    Private Function FirstRow(ByVal dt As DataTable) As DataRow
        If dt Is Nothing OrElse dt.Rows.Count = 0 Then Return Nothing
        Return dt.Rows(0)
    End Function

    '--------------------------------------------------------------------------
    ' lookups
    '--------------------------------------------------------------------------
    Public Function GetCoParms(ByVal JCCo As Byte) As DataRow
        Return FirstRow(FillTable("SELECT JCCo, COApprThreshold, DfltMarkupPct, MaxMarkupPct, DfltMaxCOPct, " & _
                                  "FieldMaxAmt, NoticeDays, FeeOnFeeYN FROM JCCoParm WHERE JCCo = @co", _
                                  New SqlParameter("@co", JCCo)))
    End Function

    Public Function GetJob(ByVal JCCo As Byte, ByVal Job As String) As DataRow
        Return FirstRow(FillTable("SELECT JCCo, Job, Description, JobStatus, ContractType, OrigContract, " & _
                                  "RevContract, TaxRate, ProjMgr, ProjExec FROM JCJob WHERE JCCo=@co AND Job=@job", _
                                  New SqlParameter("@co", JCCo), New SqlParameter("@job", Job)))
    End Function

    Public Function GetSL(ByVal SLCo As Byte, ByVal SL As String) As DataRow
        Return FirstRow(FillTable("SELECT SLCo, SL, JCCo, Job, VendGrp, Vendor, Description, SLStatus, OrigCost, " & _
                                  "CurCost, RetgPct, MaxCOPct, InsExpDate FROM SLHdr WHERE SLCo=@co AND SL=@sl", _
                                  New SqlParameter("@co", SLCo), New SqlParameter("@sl", SL)))
    End Function

    Public Function GetSLsForJob(ByVal JCCo As Byte, ByVal Job As String) As DataTable
        Return FillTable("SELECT SL, Description, SLStatus, OrigCost, CurCost, MaxCOPct FROM SLHdr " & _
                         "WHERE JCCo=@co AND Job=@job ORDER BY SL", _
                         New SqlParameter("@co", JCCo), New SqlParameter("@job", Job))
    End Function

    Public Function IsReasonActive(ByVal code As String) As Boolean
        Dim dt As DataTable = FillTable("SELECT 1 FROM JCCOReason WHERE ReasonCode=@rc AND ActiveYN='Y'", _
                                        New SqlParameter("@rc", code))
        Return dt.Rows.Count > 0
    End Function

    Public Function GetReasons() As DataTable
        Return FillTable("SELECT ReasonCode, Description FROM JCCOReason WHERE ActiveYN='Y' ORDER BY ReasonCode")
    End Function

    Public Function IsAdminUser(ByVal sUser As String) As Boolean
        Dim dt As DataTable = FillTable("SELECT 1 FROM JCAdmUsers WHERE UserName=@u", New SqlParameter("@u", sUser))
        Return dt.Rows.Count > 0
    End Function

    '--------------------------------------------------------------------------
    ' change orders
    '--------------------------------------------------------------------------
    Public Function GetCO(ByVal JCCo As Byte, ByVal Job As String, ByVal CONum As String) As DataRow
        Return FirstRow(FillTable("SELECT * FROM JCCOHdr WHERE JCCo=@co AND Job=@job AND CONum=@n", _
                                  New SqlParameter("@co", JCCo), New SqlParameter("@job", Job), _
                                  New SqlParameter("@n", CONum)))
    End Function

    Public Function GetItems(ByVal JCCo As Byte, ByVal Job As String, ByVal CONum As String) As DataTable
        Return FillTable("SELECT Item, Phase, CostType, Units, UM, UnitCost, Amount, TaxYN FROM JCCOItem " & _
                         "WHERE JCCo=@co AND Job=@job AND CONum=@n ORDER BY Item", _
                         New SqlParameter("@co", JCCo), New SqlParameter("@job", Job), _
                         New SqlParameter("@n", CONum))
    End Function

    Public Function GetNextCONum(ByVal JCCo As Byte, ByVal Job As String) As String
        Using cn As SqlConnection = GetConn()
            Using cmd As New SqlCommand("dbo.usp_JCCONextNum", cn)
                cmd.CommandType = CommandType.StoredProcedure
                cmd.Parameters.AddWithValue("@JCCo", JCCo)
                cmd.Parameters.AddWithValue("@Job", Job)
                Dim pOut As SqlParameter = cmd.Parameters.Add("@CONum", SqlDbType.VarChar, 3)
                pOut.Direction = ParameterDirection.Output
                cmd.ExecuteNonQuery()
                Return CStr(pOut.Value)
            End Using
        End Using
    End Function

    Public Function InsertHdr(ByVal r As DataRow) As Boolean
        Dim sql As String = "INSERT JCCOHdr (JCCo, Job, CONum, Description, COType, ReasonCode, Status, SL, " & _
                            "ReqDate, ReqBy, EntBy, MarkupPct, DaysExt, SourceSys) VALUES (@co, @job, @n, @d, " & _
                            "@t, @rc, 'D', @sl, @rq, @rb, @eb, @mk, @days, 'ERP')"
        Try
            Using cn As SqlConnection = GetConn()
                Using cmd As New SqlCommand(sql, cn)
                    cmd.Parameters.AddWithValue("@co", r("JCCo"))
                    cmd.Parameters.AddWithValue("@job", r("Job"))
                    cmd.Parameters.AddWithValue("@n", r("CONum"))
                    cmd.Parameters.AddWithValue("@d", r("Description"))
                    cmd.Parameters.AddWithValue("@t", r("COType"))
                    cmd.Parameters.AddWithValue("@rc", r("ReasonCode"))
                    cmd.Parameters.AddWithValue("@sl", r("SL"))
                    cmd.Parameters.AddWithValue("@rq", r("ReqDate"))
                    cmd.Parameters.AddWithValue("@rb", r("ReqBy"))
                    cmd.Parameters.AddWithValue("@eb", g_UserName)
                    cmd.Parameters.AddWithValue("@mk", r("MarkupPct"))
                    cmd.Parameters.AddWithValue("@days", r("DaysExt"))
                    cmd.ExecuteNonQuery()
                End Using
            End Using
            Return True
        Catch ex As SqlException
            m_LastErr = ex.Message
            Return False
        End Try
    End Function

    Public Function ReplaceItems(ByVal JCCo As Byte, ByVal Job As String, ByVal CONum As String, _
                                 ByVal dtItems As DataTable) As Boolean
        Try
            Using cn As SqlConnection = GetConn()
                Using tx As SqlTransaction = cn.BeginTransaction()
                    Dim del As New SqlCommand("DELETE JCCOItem WHERE JCCo=@co AND Job=@job AND CONum=@n", cn, tx)
                    del.Parameters.AddWithValue("@co", JCCo)
                    del.Parameters.AddWithValue("@job", Job)
                    del.Parameters.AddWithValue("@n", CONum)
                    del.ExecuteNonQuery()
                    Dim i As Integer = 0
                    For Each r As DataRow In dtItems.Rows
                        If r.RowState = DataRowState.Deleted Then Continue For
                        i += 1
                        Dim ins As New SqlCommand("INSERT JCCOItem (JCCo, Job, CONum, Item, Phase, CostType, Units, " & _
                                                  "UM, UnitCost, Amount, TaxYN) VALUES (@co,@job,@n,@i,@ph,@ct,@u," & _
                                                  "@um,@uc,@amt,@tx)", cn, tx)
                        ins.Parameters.AddWithValue("@co", JCCo)
                        ins.Parameters.AddWithValue("@job", Job)
                        ins.Parameters.AddWithValue("@n", CONum)
                        ins.Parameters.AddWithValue("@i", i)
                        ins.Parameters.AddWithValue("@ph", r("Phase"))
                        ins.Parameters.AddWithValue("@ct", r("CostType"))
                        ins.Parameters.AddWithValue("@u", r("Units"))
                        ins.Parameters.AddWithValue("@um", r("UM"))
                        ins.Parameters.AddWithValue("@uc", r("UnitCost"))
                        ins.Parameters.AddWithValue("@amt", r("Amount"))
                        ins.Parameters.AddWithValue("@tx", r("TaxYN"))
                        ins.ExecuteNonQuery()
                    Next
                    tx.Commit()
                End Using
            End Using
            Return True
        Catch ex As SqlException
            m_LastErr = ex.Message
            Return False
        End Try
    End Function

    ' writes the screen totals so the grid shows something before submit;
    ' usp_JCCOSubmit recomputes and overwrites them
    Public Function SaveTotals(ByVal JCCo As Byte, ByVal Job As String, ByVal CONum As String, _
                               ByVal cost As Decimal, ByVal mk As Decimal, ByVal tax As Decimal, _
                               ByVal tot As Decimal) As Boolean
        Return ExecSql("UPDATE JCCOHdr SET CostAmt=@c, MarkupAmt=@m, TaxAmt=@t, TotalAmt=@tot " & _
                       "WHERE JCCo=@co AND Job=@job AND CONum=@n", _
                       New SqlParameter("@c", cost), New SqlParameter("@m", mk), New SqlParameter("@t", tax), _
                       New SqlParameter("@tot", tot), New SqlParameter("@co", JCCo), _
                       New SqlParameter("@job", Job), New SqlParameter("@n", CONum))
    End Function

    Public Function SetStatus(ByVal JCCo As Byte, ByVal Job As String, ByVal CONum As String, _
                              ByVal sStatus As String) As Boolean
        Dim sql As String = "UPDATE JCCOHdr SET Status=@s, " & _
                            "ApprBy = CASE WHEN @s='A' THEN @u ELSE ApprBy END, " & _
                            "ApprDate = CASE WHEN @s='A' THEN GETDATE() ELSE ApprDate END " & _
                            "WHERE JCCo=@co AND Job=@job AND CONum=@n"
        Return ExecSql(sql, New SqlParameter("@s", sStatus), New SqlParameter("@u", g_UserName), _
                       New SqlParameter("@co", JCCo), New SqlParameter("@job", Job), New SqlParameter("@n", CONum))
    End Function

    Public Function VoidCO(ByVal JCCo As Byte, ByVal Job As String, ByVal CONum As String, _
                           ByVal sReason As String) As Boolean
        Return ExecSql("UPDATE JCCOHdr SET Status='V', VoidReason=@r WHERE JCCo=@co AND Job=@job AND CONum=@n", _
                       New SqlParameter("@r", sReason), New SqlParameter("@co", JCCo), _
                       New SqlParameter("@job", Job), New SqlParameter("@n", CONum))
    End Function

    Public Function ReopenCO(ByVal JCCo As Byte, ByVal Job As String, ByVal CONum As String) As Boolean
        Return ExecSql("UPDATE JCCOHdr SET Status='D', SubmitDate=NULL WHERE JCCo=@co AND Job=@job " & _
                       "AND CONum=@n AND Status='R'", _
                       New SqlParameter("@co", JCCo), New SqlParameter("@job", Job), New SqlParameter("@n", CONum))
    End Function

    '--------------------------------------------------------------------------
    ' procs
    '--------------------------------------------------------------------------
    Public Function SubmitCO(ByVal JCCo As Byte, ByVal Job As String, ByVal CONum As String, _
                             ByRef msg As String) As Integer
        Return ExecProc("dbo.usp_JCCOSubmit", msg, _
                        New SqlParameter("@JCCo", JCCo), New SqlParameter("@Job", Job), _
                        New SqlParameter("@CONum", CONum), New SqlParameter("@UserId", g_UserName))
    End Function

    Public Function ApproveCO(ByVal JCCo As Byte, ByVal Job As String, ByVal CONum As String, _
                              ByVal sAction As String, ByVal sReason As String, ByVal bOverride As Boolean, _
                              ByRef msg As String) As Integer
        Return ExecProc("dbo.usp_JCCOApprove", msg, _
                        New SqlParameter("@JCCo", JCCo), New SqlParameter("@Job", Job), _
                        New SqlParameter("@CONum", CONum), New SqlParameter("@Action", sAction), _
                        New SqlParameter("@Reason", IIf(sReason Is Nothing, DBNull.Value, sReason)), _
                        New SqlParameter("@UserId", g_UserName), _
                        New SqlParameter("@Override", IIf(bOverride, "Y", "N")))
    End Function

    Public Function InterfaceCO(ByVal JCCo As Byte, ByVal Job As String, ByVal CONum As String, _
                                ByRef msg As String) As Integer
        Return ExecProc("dbo.usp_JCCOInterface", msg, _
                        New SqlParameter("@JCCo", JCCo), New SqlParameter("@Job", Job), _
                        New SqlParameter("@CONum", CONum), New SqlParameter("@UserId", g_UserName))
    End Function

    Public Function FieldImport(ByVal p As Dictionary(Of String, Object), ByVal itemsXml As String, _
                                ByRef coNum As String, ByRef msg As String) As Integer
        Dim pCO As New SqlParameter("@CONum", SqlDbType.VarChar, 3)
        pCO.Direction = ParameterDirection.Output
        Dim rc As Integer = ExecProc("dbo.usp_JCCOFieldImport", msg, _
                        New SqlParameter("@JCCo", g_JCCo), New SqlParameter("@Job", p("job")), _
                        New SqlParameter("@ExtRef", p("extRef")), New SqlParameter("@Description", p("description")), _
                        New SqlParameter("@COType", p("coType")), New SqlParameter("@ReasonCode", p("reasonCode")), _
                        New SqlParameter("@SL", IIf(p.ContainsKey("subcontract"), p("subcontract"), DBNull.Value)), _
                        New SqlParameter("@ReqDate", p("requestDate")), New SqlParameter("@ReqBy", p("requestedBy")), _
                        New SqlParameter("@DaysExt", IIf(p.ContainsKey("daysExtension"), p("daysExtension"), 0)), _
                        New SqlParameter("@ItemsXml", itemsXml), pCO)
        coNum = IIf(IsDBNull(pCO.Value), "", CStr(pCO.Value))
        Return rc
    End Function

    '--------------------------------------------------------------------------
    ' queues
    '--------------------------------------------------------------------------
    Public Function GetPendingQueue(ByVal JCCo As Byte, ByVal bAll As Boolean) As DataTable
        Dim sql As String = "SELECT h.Job, h.CONum, h.Description, h.COType, h.ReasonCode, h.TotalAmt, h.EntBy, " & _
                            "h.ReqBy, h.SubmitDate, h.SourceSys, h.SL, j.ProjMgr FROM JCCOHdr h JOIN JCJob j " & _
                            "ON j.JCCo = h.JCCo AND j.Job = h.Job WHERE h.JCCo=@co AND h.Status='P'"
        If Not bAll Then sql &= " AND (j.ProjMgr=@u OR j.ProjExec=@u)"
        sql &= " ORDER BY h.SubmitDate"
        Return FillTable(sql, New SqlParameter("@co", JCCo), New SqlParameter("@u", g_UserName))
    End Function

    Public Function GetExportable(ByVal JCCo As Byte) As DataTable
        Return FillTable("SELECT h.*, j.ContractType FROM JCCOHdr h JOIN JCJob j ON j.JCCo=h.JCCo AND j.Job=h.Job " & _
                         "WHERE h.JCCo=@co AND h.Status IN ('A','I') AND h.ExpFlag='N' AND h.COType <> 'I' " & _
                         "ORDER BY h.Job, h.CONum", New SqlParameter("@co", JCCo))
    End Function

    Public Function MarkExported(ByVal JCCo As Byte, ByVal Job As String, ByVal CONum As String) As Boolean
        Return ExecSql("UPDATE JCCOHdr SET ExpFlag='Y', ExpDate=GETDATE() WHERE JCCo=@co AND Job=@job AND CONum=@n", _
                       New SqlParameter("@co", JCCo), New SqlParameter("@job", Job), New SqlParameter("@n", CONum))
    End Function

    Public Function GetSLCOSummary(ByVal SLCo As Byte, ByVal SL As String) As DataTable
        Return FillTable("SELECT CONum, Description, Status, CostAmt, ApprDate FROM JCCOHdr " & _
                         "WHERE JCCo=@co AND SL=@sl AND COType='S' ORDER BY CONum", _
                         New SqlParameter("@co", SLCo), New SqlParameter("@sl", SL))
    End Function

    Public Function SaveSL(ByVal r As DataRow) As Boolean
        Return ExecSql("UPDATE SLHdr SET Description=@d, SLStatus=@s, RetgPct=@rp, MaxCOPct=@mx, InsExpDate=@ie " & _
                       "WHERE SLCo=@co AND SL=@sl", _
                       New SqlParameter("@d", r("Description")), New SqlParameter("@s", r("SLStatus")), _
                       New SqlParameter("@rp", r("RetgPct")), New SqlParameter("@mx", r("MaxCOPct")), _
                       New SqlParameter("@ie", r("InsExpDate")), New SqlParameter("@co", r("SLCo")), _
                       New SqlParameter("@sl", r("SL")))
    End Function

    Public Function CountOpenCOsForSL(ByVal SLCo As Byte, ByVal SL As String) As Integer
        Dim dt As DataTable = FillTable("SELECT COUNT(*) FROM JCCOHdr WHERE JCCo=@co AND SL=@sl AND Status IN ('D','P','A')", _
                                        New SqlParameter("@co", SLCo), New SqlParameter("@sl", SL))
        Return CInt(dt.Rows(0)(0))
    End Function

    '--------------------------------------------------------------------------
    ' helpers
    '--------------------------------------------------------------------------
    Private Function ExecSql(ByVal sql As String, ByVal ParamArray prms() As SqlParameter) As Boolean
        Try
            Using cn As SqlConnection = GetConn()
                Using cmd As New SqlCommand(sql, cn)
                    For Each p As SqlParameter In prms
                        cmd.Parameters.Add(p)
                    Next
                    cmd.ExecuteNonQuery()
                End Using
            End Using
            Return True
        Catch ex As SqlException
            m_LastErr = ex.Message
            Return False
        End Try
    End Function

    Private Function ExecProc(ByVal procName As String, ByRef msg As String, _
                              ByVal ParamArray prms() As SqlParameter) As Integer
        Try
            Using cn As SqlConnection = GetConn()
                Using cmd As New SqlCommand(procName, cn)
                    cmd.CommandType = CommandType.StoredProcedure
                    For Each p As SqlParameter In prms
                        cmd.Parameters.Add(p)
                    Next
                    Dim pMsg As SqlParameter = cmd.Parameters.Add("@msg", SqlDbType.VarChar, 255)
                    pMsg.Direction = ParameterDirection.Output
                    Dim pRet As SqlParameter = cmd.Parameters.Add("@ret", SqlDbType.Int)
                    pRet.Direction = ParameterDirection.ReturnValue
                    cmd.ExecuteNonQuery()
                    msg = IIf(IsDBNull(pMsg.Value), "", CStr(pMsg.Value))
                    Return CInt(pRet.Value)
                End Using
            End Using
        Catch ex As SqlException
            msg = ex.Message
            m_LastErr = ex.Message
            Return 99
        End Try
    End Function

End Class
