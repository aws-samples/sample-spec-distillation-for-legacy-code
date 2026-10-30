' Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
'==============================================================================
' frmAPInvoiceApproval.vb
' Approval queue. Lists Pending Approval invoices, approver can Approve or
' Reject. Final say is usp_APInvApprove (limits, SoD, auto holds).
'   11/2009 KLT  created
'   06/2013 PSV  show auto-hold result after approve
'==============================================================================
Option Strict Off
Option Explicit On

Imports System.Data
Imports System.Data.SqlClient
Imports System.Windows.Forms

Public Class frmAPInvoiceApproval
    Inherits System.Windows.Forms.Form

    Friend WithEvents grdQueue As New DataGridView
    Friend WithEvents btnApprove As New Button
    Friend WithEvents btnReject As New Button
    Friend WithEvents btnRefresh As New Button
    Friend WithEvents txtReason As New TextBox
    Friend WithEvents lblLimit As New Label
    Friend WithEvents chkShowAll As New CheckBox

    Private m_BL As clsAPInvoiceBL
    Private m_dtQ As DataTable

    Private Sub frmAPInvoiceApproval_Load(ByVal sender As Object, ByVal e As System.EventArgs) Handles MyBase.Load
        m_BL = New clsAPInvoiceBL
        Me.Text = "AP Invoice Approval"
        lblLimit.Text = "Your approval limit: " & Format(GetRoleApprLimit(g_UserRole), "$#,##0.00")
        If g_UserRole = ROLE_CLERK Then
            ' clerks can look but not approve
            btnApprove.Enabled = False
            btnReject.Enabled = False
        End If
        LoadQueue()
    End Sub

    Private Sub LoadQueue()
        ' PM sees only invoices on jobs where they are PM; CTRL/CFO see everything
        Dim sql As String = _
            "SELECT h.Mth, h.APTrans, h.VendGrp, h.Vendor, v.Name, h.APRef, h.InvDate, h.GrossAmt, h.RetgAmt, " & _
            "       h.Job, h.EntBy, h.EntDate " & _
            "FROM APInvHdr h JOIN APVend v ON v.VendGrp=h.VendGrp AND v.Vendor=h.Vendor " & _
            "LEFT JOIN JCJob j ON j.JCCo=h.APCo AND j.Job=h.Job " & _
            "WHERE h.APCo=@co AND h.Status='P' "
        If g_UserRole = ROLE_PM AndAlso Not chkShowAll.Checked Then
            sql &= " AND j.ProjMgr = @u "
        End If
        sql &= " ORDER BY h.InvDate"
        m_dtQ = New DataTable
        Using cn As SqlConnection = GetConn()
            Using cmd As New SqlCommand(sql, cn)
                cmd.Parameters.AddWithValue("@co", g_APCo)
                cmd.Parameters.AddWithValue("@u", g_UserId)
                Dim da As New SqlDataAdapter(cmd)
                da.Fill(m_dtQ)
            End Using
        End Using
        grdQueue.DataSource = m_dtQ
        ' grey out rows over the user's limit so they don't bother clicking
        Dim lim As Decimal = GetRoleApprLimit(g_UserRole)
        For Each gr As DataGridViewRow In grdQueue.Rows
            If gr.DataBoundItem Is Nothing Then Continue For
            If CDec(gr.Cells("GrossAmt").Value) > lim Then
                gr.DefaultCellStyle.ForeColor = Drawing.Color.Gray
            End If
        Next
    End Sub

    Private Function CurRow() As DataRow
        If grdQueue.CurrentRow Is Nothing OrElse grdQueue.CurrentRow.DataBoundItem Is Nothing Then Return Nothing
        Return CType(grdQueue.CurrentRow.DataBoundItem, DataRowView).Row
    End Function

    Private Sub btnApprove_Click(ByVal sender As Object, ByVal e As System.EventArgs) Handles btnApprove.Click
        Dim r As DataRow = CurRow()
        If r Is Nothing Then Exit Sub
        Dim sWhy As String = ""
        If Not m_BL.CanApprove(g_UserRole, CDec(r("GrossAmt")), r("EntBy").ToString(), sWhy) Then
            MsgBox(sWhy, MsgBoxStyle.Exclamation)
            Exit Sub
        End If

        Dim sMsg As String = ""
        Dim rc As Integer = m_BL.DAL.ApproveInvoice(g_APCo, CDate(r("Mth")), CInt(r("APTrans")), "A", "", sMsg)
        Select Case rc
            Case 0
                ' approved clean
            Case 1
                ' approved but auto-held (lien waiver / insurance)
                MsgBox("Approved and placed on hold: " & sMsg, MsgBoxStyle.Information)
            Case Else
                MsgBox("Approval failed (" & rc & "): " & sMsg, MsgBoxStyle.Critical)
        End Select
        LoadQueue()
    End Sub

    Private Sub btnReject_Click(ByVal sender As Object, ByVal e As System.EventArgs) Handles btnReject.Click
        Dim r As DataRow = CurRow()
        If r Is Nothing Then Exit Sub
        If Trim(txtReason.Text) = "" Then
            MsgBox("Enter a reason for the rejection.", MsgBoxStyle.Exclamation)
            txtReason.Focus()
            Exit Sub
        End If
        Dim sMsg As String = ""
        ' reject sends it back to Open for the clerk to fix / void
        Dim rc As Integer = m_BL.DAL.ApproveInvoice(g_APCo, CDate(r("Mth")), CInt(r("APTrans")), "R", _
                                                     Trim(txtReason.Text), sMsg)
        If rc <> 0 Then MsgBox("Reject failed (" & rc & "): " & sMsg, MsgBoxStyle.Critical)
        txtReason.Text = ""
        LoadQueue()
    End Sub

    Private Sub btnRefresh_Click(ByVal sender As Object, ByVal e As System.EventArgs) Handles btnRefresh.Click
        LoadQueue()
    End Sub

    Private Sub chkShowAll_CheckedChanged(ByVal sender As Object, ByVal e As System.EventArgs) Handles chkShowAll.CheckedChanged
        LoadQueue()
    End Sub

    'Private Sub btnApproveAll_Click(...)  -- removed 2012, approvers were bulk approving w/o review (KLT)
    '    For Each r In m_dtQ.Rows
    '        m_BL.DAL.ApproveInvoice(g_APCo, r("Mth"), r("APTrans"), "A", "", sMsg)
    '    Next
    'End Sub

End Class
