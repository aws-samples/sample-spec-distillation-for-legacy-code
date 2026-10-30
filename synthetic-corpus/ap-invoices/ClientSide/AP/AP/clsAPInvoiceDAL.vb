' Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
'==============================================================================
' clsAPInvoiceDAL.vb
' Data access for AP invoice header / lines / holds.
' All the column names are the db ones - see Scripts\Tables\AP_Tables.sql
'   APCo   = AP company          Mth    = batch/posting month (1st of month)
'   APTrans= trans # within Mth   UIMth  = month the user had open in the UI
'   APRef  = vendor invoice #     VendGrp/Vendor = vendor key
'   GrossAmt includes tax.  RetgAmt = retainage withheld
'==============================================================================
Option Strict Off
Option Explicit On

Imports System.Data
Imports System.Data.SqlClient

Public Class clsAPInvoiceDAL

    Private m_LastErr As String = ""

    Public ReadOnly Property LastError() As String
        Get
            Return m_LastErr
        End Get
    End Property

    '--------------------------------------------------------------------------
    ' company parameters
    '--------------------------------------------------------------------------
    Public Function GetCoParms(ByVal APCo As Byte) As DataRow
        Dim sql As String = "SELECT APCo, GLCo, ApprThreshold, DupDays, LienWvrThreshold, " & _
                            "DfltRetgPct, LastMthSubClsd, AllowFutureInv FROM APCoParm WHERE APCo = @APCo"
        Dim dt As New DataTable
        Using cn As SqlConnection = GetConn()
            Using cmd As New SqlCommand(sql, cn)
                cmd.Parameters.AddWithValue("@APCo", APCo)
                Dim da As New SqlDataAdapter(cmd)
                da.Fill(dt)
            End Using
        End Using
        If dt.Rows.Count = 0 Then Return Nothing
        Return dt.Rows(0)
    End Function

    Public Function GetVendor(ByVal VendGrp As Byte, ByVal Vendor As Integer) As DataRow
        Dim sql As String = "SELECT VendGrp, Vendor, Name, ActiveYN, V1099YN, TIN, DfltRetgPct, " & _
                            "PayTerms, VendType FROM APVend WHERE VendGrp=@vg AND Vendor=@v"
        Dim dt As New DataTable
        Using cn As SqlConnection = GetConn()
            Using cmd As New SqlCommand(sql, cn)
                cmd.Parameters.AddWithValue("@vg", VendGrp)
                cmd.Parameters.AddWithValue("@v", Vendor)
                Dim da As New SqlDataAdapter(cmd)
                da.Fill(dt)
            End Using
        End Using
        If dt.Rows.Count = 0 Then Return Nothing
        Return dt.Rows(0)
    End Function

    Public Function GetVendCompliance(ByVal VendGrp As Byte, ByVal Vendor As Integer) As DataTable
        Dim sql As String = "SELECT CompCode, ExpDate, VerifiedYN FROM APVendComp " & _
                            "WHERE VendGrp=@vg AND Vendor=@v AND CompCode IN ('GL','WC')"
        Dim dt As New DataTable
        Using cn As SqlConnection = GetConn()
            Using cmd As New SqlCommand(sql, cn)
                cmd.Parameters.AddWithValue("@vg", VendGrp)
                cmd.Parameters.AddWithValue("@v", Vendor)
                Dim da As New SqlDataAdapter(cmd)
                da.Fill(dt)
            End Using
        End Using
        Return dt
    End Function

    Public Function GetJobStatus(ByVal JCCo As Byte, ByVal Job As String) As Integer
        Using cn As SqlConnection = GetConn()
            Using cmd As New SqlCommand("SELECT JobStatus FROM JCJob WHERE JCCo=@c AND Job=@j", cn)
                cmd.Parameters.AddWithValue("@c", JCCo)
                cmd.Parameters.AddWithValue("@j", Job)
                Dim o As Object = cmd.ExecuteScalar()
                If o Is Nothing OrElse IsDBNull(o) Then Return -1
                Return CInt(o)
            End Using
        End Using
    End Function

    '--------------------------------------------------------------------------
    ' header / lines
    '--------------------------------------------------------------------------
    Public Function GetInvoice(ByVal APCo As Byte, ByVal Mth As Date, ByVal APTrans As Integer) As DataRow
        Dim sql As String = "SELECT * FROM APInvHdr WHERE APCo=@co AND Mth=@m AND APTrans=@t"
        Dim dt As New DataTable
        Using cn As SqlConnection = GetConn()
            Using cmd As New SqlCommand(sql, cn)
                cmd.Parameters.AddWithValue("@co", APCo)
                cmd.Parameters.AddWithValue("@m", Mth)
                cmd.Parameters.AddWithValue("@t", APTrans)
                Dim da As New SqlDataAdapter(cmd)
                da.Fill(dt)
            End Using
        End Using
        If dt.Rows.Count = 0 Then Return Nothing
        Return dt.Rows(0)
    End Function

    Public Function GetInvLines(ByVal APCo As Byte, ByVal Mth As Date, ByVal APTrans As Integer) As DataTable
        Dim sql As String = "SELECT APLine, LineType, JCCo, Job, Phase, GLAcct, Amount, TaxAmt, RetgPct, RetgAmt, Descr " & _
                            "FROM APInvLine WHERE APCo=@co AND Mth=@m AND APTrans=@t ORDER BY APLine"
        Dim dt As New DataTable
        Using cn As SqlConnection = GetConn()
            Using cmd As New SqlCommand(sql, cn)
                cmd.Parameters.AddWithValue("@co", APCo)
                cmd.Parameters.AddWithValue("@m", Mth)
                cmd.Parameters.AddWithValue("@t", APTrans)
                Dim da As New SqlDataAdapter(cmd)
                da.Fill(dt)
            End Using
        End Using
        Return dt
    End Function

    ' returns new APTrans or -1
    Public Function InsertInvoiceHdr(ByVal APCo As Byte, ByVal Mth As Date, ByVal UIMth As Date, _
                                     ByVal VendGrp As Byte, ByVal Vendor As Integer, ByVal APRef As String, _
                                     ByVal InvDate As Date, ByVal DueDate As Date, ByVal PayTerms As Integer, _
                                     ByVal Descr As String, ByVal GrossAmt As Decimal, ByVal TaxAmt As Decimal, _
                                     ByVal LienWvrRcvd As String, ByVal Job As String) As Integer
        Dim sql As String = _
            "DECLARE @t int; " & _
            "SELECT @t = ISNULL(MAX(APTrans),0)+1 FROM APInvHdr WITH (UPDLOCK) WHERE APCo=@co AND Mth=@m; " & _
            "INSERT APInvHdr (APCo, Mth, APTrans, UIMth, VendGrp, Vendor, APRef, InvDate, DueDate, PayTerms, " & _
            "  Descr, GrossAmt, TaxAmt, RetgAmt, Status, LienWvrRcvd, Job, EntBy, EntDate) " & _
            "VALUES (@co, @m, @t, @uim, @vg, @v, @ref, @idt, @ddt, @pt, @d, @g, @tx, 0, 'O', @lw, @job, SUSER_SNAME(), GETDATE()); " & _
            "SELECT @t;"
        Try
            Using cn As SqlConnection = GetConn()
                Using cmd As New SqlCommand(sql, cn)
                    cmd.Parameters.AddWithValue("@co", APCo)
                    cmd.Parameters.AddWithValue("@m", Mth)
                    cmd.Parameters.AddWithValue("@uim", UIMth)
                    cmd.Parameters.AddWithValue("@vg", VendGrp)
                    cmd.Parameters.AddWithValue("@v", Vendor)
                    cmd.Parameters.AddWithValue("@ref", APRef)
                    cmd.Parameters.AddWithValue("@idt", InvDate)
                    cmd.Parameters.AddWithValue("@ddt", DueDate)
                    cmd.Parameters.AddWithValue("@pt", PayTerms)
                    cmd.Parameters.AddWithValue("@d", Descr)
                    cmd.Parameters.AddWithValue("@g", GrossAmt)
                    cmd.Parameters.AddWithValue("@tx", TaxAmt)
                    cmd.Parameters.AddWithValue("@lw", LienWvrRcvd)
                    cmd.Parameters.AddWithValue("@job", IIf(Job = "", DBNull.Value, Job))
                    Return CInt(cmd.ExecuteScalar())
                End Using
            End Using
        Catch ex As SqlException
            m_LastErr = ex.Message
            Return -1
        End Try
    End Function

    Public Function InsertInvLine(ByVal APCo As Byte, ByVal Mth As Date, ByVal APTrans As Integer, ByVal APLine As Short, _
                                  ByVal LineType As Integer, ByVal Job As String, ByVal Phase As String, ByVal GLAcct As String, _
                                  ByVal Amount As Decimal, ByVal TaxAmt As Decimal, ByVal RetgPct As Decimal, _
                                  ByVal RetgAmt As Decimal, ByVal Descr As String) As Boolean
        Dim sql As String = "INSERT APInvLine (APCo, Mth, APTrans, APLine, LineType, JCCo, Job, Phase, GLAcct, " & _
                            "Amount, TaxAmt, RetgPct, RetgAmt, Descr) VALUES (@co,@m,@t,@l,@lt,@co,@job,@ph,@gl,@amt,@tx,@rp,@ra,@d)"
        Try
            Using cn As SqlConnection = GetConn()
                Using cmd As New SqlCommand(sql, cn)
                    cmd.Parameters.AddWithValue("@co", APCo)
                    cmd.Parameters.AddWithValue("@m", Mth)
                    cmd.Parameters.AddWithValue("@t", APTrans)
                    cmd.Parameters.AddWithValue("@l", APLine)
                    cmd.Parameters.AddWithValue("@lt", LineType)
                    cmd.Parameters.AddWithValue("@job", IIf(Job = "", DBNull.Value, Job))
                    cmd.Parameters.AddWithValue("@ph", IIf(Phase = "", DBNull.Value, Phase))
                    cmd.Parameters.AddWithValue("@gl", IIf(GLAcct = "", DBNull.Value, GLAcct))
                    cmd.Parameters.AddWithValue("@amt", Amount)
                    cmd.Parameters.AddWithValue("@tx", TaxAmt)
                    cmd.Parameters.AddWithValue("@rp", RetgPct)
                    cmd.Parameters.AddWithValue("@ra", RetgAmt)
                    cmd.Parameters.AddWithValue("@d", Descr)
                    cmd.ExecuteNonQuery()
                End Using
            End Using
            Return True
        Catch ex As SqlException
            m_LastErr = ex.Message
            Return False
        End Try
    End Function

    '--------------------------------------------------------------------------
    ' stored proc wrappers
    '--------------------------------------------------------------------------
    Public Function DupCheck(ByVal APCo As Byte, ByVal VendGrp As Byte, ByVal Vendor As Integer, ByVal APRef As String, _
                             ByVal GrossAmt As Decimal, ByVal InvDate As Date, ByVal ExclMth As Object, _
                             ByVal ExclTrans As Object) As DataTable
        Dim dt As New DataTable
        Using cn As SqlConnection = GetConn()
            Using cmd As New SqlCommand("usp_APDupCheck", cn)
                cmd.CommandType = CommandType.StoredProcedure
                cmd.Parameters.AddWithValue("@APCo", APCo)
                cmd.Parameters.AddWithValue("@VendGrp", VendGrp)
                cmd.Parameters.AddWithValue("@Vendor", Vendor)
                cmd.Parameters.AddWithValue("@APRef", APRef)
                cmd.Parameters.AddWithValue("@GrossAmt", GrossAmt)
                cmd.Parameters.AddWithValue("@InvDate", InvDate)
                cmd.Parameters.AddWithValue("@ExclMth", IIf(ExclMth Is Nothing, DBNull.Value, ExclMth))
                cmd.Parameters.AddWithValue("@ExclTrans", IIf(ExclTrans Is Nothing, DBNull.Value, ExclTrans))
                Dim da As New SqlDataAdapter(cmd)
                da.Fill(dt)
            End Using
        End Using
        Return dt
    End Function

    Public Function PostInvoice(ByVal APCo As Byte, ByVal Mth As Date, ByVal APTrans As Integer, _
                                ByVal DupOvrdBy As String, ByRef sMsg As String) As Integer
        Return ExecProcRC("usp_APInvPost", sMsg, _
            New SqlParameter("@APCo", APCo), New SqlParameter("@Mth", Mth), _
            New SqlParameter("@APTrans", APTrans), _
            New SqlParameter("@DupOvrdBy", IIf(DupOvrdBy = "", DBNull.Value, DupOvrdBy)))
    End Function

    Public Function ApproveInvoice(ByVal APCo As Byte, ByVal Mth As Date, ByVal APTrans As Integer, _
                                   ByVal Action As String, ByVal Reason As String, ByRef sMsg As String) As Integer
        Return ExecProcRC("usp_APInvApprove", sMsg, _
            New SqlParameter("@APCo", APCo), New SqlParameter("@Mth", Mth), _
            New SqlParameter("@APTrans", APTrans), New SqlParameter("@Action", Action), _
            New SqlParameter("@Reason", IIf(Reason = "", DBNull.Value, Reason)), _
            New SqlParameter("@UserId", g_UserId))
    End Function

    Public Function ReleaseHold(ByVal APCo As Byte, ByVal Mth As Date, ByVal APTrans As Integer, _
                                ByVal HoldCode As String, ByVal Notes As String, ByRef sMsg As String) As Integer
        Return ExecProcRC("usp_APHoldRelease", sMsg, _
            New SqlParameter("@APCo", APCo), New SqlParameter("@Mth", Mth), _
            New SqlParameter("@APTrans", APTrans), New SqlParameter("@HoldCode", HoldCode), _
            New SqlParameter("@UserId", g_UserId), New SqlParameter("@Notes", Notes))
    End Function

    Public Function AddHold(ByVal APCo As Byte, ByVal Mth As Date, ByVal APTrans As Integer, _
                            ByVal HoldCode As String, ByVal Notes As String) As Boolean
        ' manual holds only - auto holds (LW/INS) are put on by usp_APInvApprove
        Dim sql As String = _
            "INSERT APHold (APCo, Mth, APTrans, HoldCode, HoldDate, HoldBy, Notes) " & _
            "VALUES (@co,@m,@t,@h,GETDATE(),@u,@n); " & _
            "UPDATE APInvHdr SET Status='H' WHERE APCo=@co AND Mth=@m AND APTrans=@t AND Status IN ('A','H');"
        Try
            Using cn As SqlConnection = GetConn()
                Using cmd As New SqlCommand(sql, cn)
                    cmd.Parameters.AddWithValue("@co", APCo)
                    cmd.Parameters.AddWithValue("@m", Mth)
                    cmd.Parameters.AddWithValue("@t", APTrans)
                    cmd.Parameters.AddWithValue("@h", HoldCode)
                    cmd.Parameters.AddWithValue("@u", g_UserId)
                    cmd.Parameters.AddWithValue("@n", Notes)
                    cmd.ExecuteNonQuery()
                End Using
            End Using
            Return True
        Catch ex As SqlException
            m_LastErr = ex.Message
            Return False
        End Try
    End Function

    Public Function GetOpenHolds(ByVal APCo As Byte, ByVal Mth As Date, ByVal APTrans As Integer) As DataTable
        Dim sql As String = "SELECT h.HoldCode, c.Descr, c.RelRole, h.HoldDate, h.HoldBy, h.Notes " & _
                            "FROM APHold h JOIN APHoldCode c ON c.HoldCode = h.HoldCode " & _
                            "WHERE h.APCo=@co AND h.Mth=@m AND h.APTrans=@t AND h.RelDate IS NULL"
        Dim dt As New DataTable
        Using cn As SqlConnection = GetConn()
            Using cmd As New SqlCommand(sql, cn)
                cmd.Parameters.AddWithValue("@co", APCo)
                cmd.Parameters.AddWithValue("@m", Mth)
                cmd.Parameters.AddWithValue("@t", APTrans)
                Dim da As New SqlDataAdapter(cmd)
                da.Fill(dt)
            End Using
        End Using
        Return dt
    End Function

    Public Function VoidInvoice(ByVal APCo As Byte, ByVal Mth As Date, ByVal APTrans As Integer, _
                                ByVal Reason As String) As Boolean
        ' trigger trg_APInvHdr_u will reject if status not allowed
        Dim sql As String = "UPDATE APInvHdr SET Status='V', VoidReason=@r, VoidBy=@u, VoidDate=GETDATE() " & _
                            "WHERE APCo=@co AND Mth=@m AND APTrans=@t"
        Try
            Using cn As SqlConnection = GetConn()
                Using cmd As New SqlCommand(sql, cn)
                    cmd.Parameters.AddWithValue("@co", APCo)
                    cmd.Parameters.AddWithValue("@m", Mth)
                    cmd.Parameters.AddWithValue("@t", APTrans)
                    cmd.Parameters.AddWithValue("@r", Reason)
                    cmd.Parameters.AddWithValue("@u", g_UserId)
                    cmd.ExecuteNonQuery()
                End Using
            End Using
            Return True
        Catch ex As SqlException
            m_LastErr = ex.Message
            Return False
        End Try
    End Function

    ' OBSOLETE - discount terms moved to APPayTerms table (2011). proc no longer deployed.
    Public Function GetDiscTerms(ByVal VendGrp As Byte, ByVal Vendor As Integer) As DataRow
        Dim dt As New DataTable
        Using cn As SqlConnection = GetConn()
            Using cmd As New SqlCommand("usp_APDiscTerms_OLD", cn)
                cmd.CommandType = CommandType.StoredProcedure
                cmd.Parameters.AddWithValue("@VendGrp", VendGrp)
                cmd.Parameters.AddWithValue("@Vendor", Vendor)
                Dim da As New SqlDataAdapter(cmd)
                da.Fill(dt)
            End Using
        End Using
        If dt.Rows.Count = 0 Then Return Nothing
        Return dt.Rows(0)
    End Function

    '--------------------------------------------------------------------------
    Private Function ExecProcRC(ByVal procName As String, ByRef sMsg As String, _
                                ByVal ParamArray prms() As SqlParameter) As Integer
        sMsg = ""
        Try
            Using cn As SqlConnection = GetConn()
                Using cmd As New SqlCommand(procName, cn)
                    cmd.CommandType = CommandType.StoredProcedure
                    For Each p As SqlParameter In prms
                        cmd.Parameters.Add(p)
                    Next
                    Dim pMsg As New SqlParameter("@msg", SqlDbType.VarChar, 255)
                    pMsg.Direction = ParameterDirection.Output
                    cmd.Parameters.Add(pMsg)
                    Dim pRC As New SqlParameter("@rc", SqlDbType.Int)
                    pRC.Direction = ParameterDirection.ReturnValue
                    cmd.Parameters.Add(pRC)
                    cmd.ExecuteNonQuery()
                    sMsg = IIf(IsDBNull(pMsg.Value), "", pMsg.Value)
                    Return CInt(pRC.Value)
                End Using
            End Using
        Catch ex As SqlException
            sMsg = ex.Message
            m_LastErr = ex.Message
            Return 99
        End Try
    End Function

End Class
