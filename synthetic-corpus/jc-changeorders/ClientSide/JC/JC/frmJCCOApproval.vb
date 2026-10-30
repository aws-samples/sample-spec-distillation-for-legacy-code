' Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
'==============================================================================
' frmJCCOApproval.vb
' Approval queue. Lists Pending Approval COs for the jobs where the user is
' PM or PX (all jobs with Show All). Approve / Reject, then Post approved COs
' to the contract / subcontract. Final say is usp_JCCOApprove.
'   02/2010 MJR  created
'   05/2018 TKB  support desk
'==============================================================================
Option Strict Off
Option Explicit On

Imports System.Data
Imports System.Windows.Forms

Public Class frmJCCOApproval
    Inherits System.Windows.Forms.Form

    Friend WithEvents grdQueue As New DataGridView
    Friend WithEvents btnApprove As New Button
    Friend WithEvents btnReject As New Button
    Friend WithEvents btnPost As New Button
    Friend WithEvents btnRefresh As New Button
    Friend WithEvents txtReason As New TextBox
    Friend WithEvents lblLimit As New Label
    Friend WithEvents chkShowAll As New CheckBox

    Private m_BL As clsJCCOBL
    Private m_DAL As clsJCCODAL
    Private m_dtQ As DataTable
    Private m_Ovr As Boolean = False

    Private Sub frmJCCOApproval_Load(ByVal sender As Object, ByVal e As System.EventArgs) Handles MyBase.Load
        m_DAL = New clsJCCODAL
        m_BL = New clsJCCOBL(m_DAL)
        Me.Text = "JC Change Order Approval"
        Me.KeyPreview = True
        lblLimit.Text = "Your approval limit: " & Format(GetRoleApprLimit(g_UserRole), "$#,##0.00")
        If g_UserRole = ROLE_ENTRY OrElse g_UserRole = "" Then
            btnApprove.Enabled = False
            btnReject.Enabled = False
            btnPost.Enabled = False
        End If
        chkShowAll.Visible = (g_UserRole = ROLE_VP OrElse g_UserRole = ROLE_CFO)
        LoadQueue()
    End Sub

    Private Sub LoadQueue()
        m_dtQ = m_DAL.GetPendingQueue(g_JCCo, chkShowAll.Checked)
        grdQueue.DataSource = m_dtQ
    End Sub

    Private Sub chkShowAll_CheckedChanged(ByVal sender As Object, ByVal e As System.EventArgs) Handles chkShowAll.CheckedChanged
        LoadQueue()
    End Sub

    Private Sub btnRefresh_Click(ByVal sender As Object, ByVal e As System.EventArgs) Handles btnRefresh.Click
        LoadQueue()
    End Sub

    Private Sub frmJCCOApproval_KeyDown(ByVal sender As Object, ByVal e As KeyEventArgs) Handles MyBase.KeyDown
        ' support desk
        If e.Control AndAlso e.Shift AndAlso e.KeyCode = Keys.F12 Then
            If m_DAL.IsAdminUser(g_UserName) Then
                m_Ovr = Not m_Ovr
                Me.Text = IIf(m_Ovr, "JC Change Order Approval.", "JC Change Order Approval")
            End If
            e.Handled = True
        End If
    End Sub

    Private Function SelRow() As DataRow
        If grdQueue.CurrentRow Is Nothing Then Return Nothing
        Return CType(grdQueue.CurrentRow.DataBoundItem, DataRowView).Row
    End Function

    Private Sub btnApprove_Click(ByVal sender As Object, ByVal e As System.EventArgs) Handles btnApprove.Click
        Dim q As DataRow = SelRow()
        If q Is Nothing Then Return
        Dim hdr As DataRow = m_DAL.GetCO(g_JCCo, CStr(q("Job")), CStr(q("CONum")))
        If Not m_Ovr AndAlso Not m_BL.CanApprove(hdr, g_UserName, g_UserRole) Then
            MessageBox.Show(m_BL.ErrorText, Me.Text)
            Return
        End If
        Dim msg As String = ""
        Dim rc As Integer = m_DAL.ApproveCO(g_JCCo, CStr(q("Job")), CStr(q("CONum")), "A", Nothing, m_Ovr, msg)
        Select Case rc
            Case 0
                MessageBox.Show("Change order " & CStr(q("CONum")) & " approved.", Me.Text)
            Case 3
                MessageBox.Show("You are not authorized to approve this change order." & vbCrLf & msg, Me.Text)
            Case 4
                MessageBox.Show("Amount is over your approval limit." & vbCrLf & msg, Me.Text)
            Case Else
                MessageBox.Show(msg, Me.Text)
        End Select
        LoadQueue()
    End Sub

    Private Sub btnReject_Click(ByVal sender As Object, ByVal e As System.EventArgs) Handles btnReject.Click
        Dim q As DataRow = SelRow()
        If q Is Nothing Then Return
        If txtReason.Text.Trim() = "" Then
            MessageBox.Show("Enter a rejection reason.", Me.Text)
            Return
        End If
        Dim msg As String = ""
        Dim rc As Integer = m_DAL.ApproveCO(g_JCCo, CStr(q("Job")), CStr(q("CONum")), "R", txtReason.Text.Trim(), False, msg)
        If rc <> 0 Then
            MessageBox.Show(msg, Me.Text)
        End If
        txtReason.Text = ""
        LoadQueue()
    End Sub

    ' post approved COs (A -> I). Owner COs raise the revised contract,
    ' sub COs raise the subcontract and add committed cost.
    Private Sub btnPost_Click(ByVal sender As Object, ByVal e As System.EventArgs) Handles btnPost.Click
        Dim sJob As String = InputBox("Job", Me.Text)
        Dim sCO As String = InputBox("Change order #", Me.Text)
        If sJob = "" OrElse sCO = "" Then Return
        Dim msg As String = ""
        Dim rc As Integer = m_DAL.InterfaceCO(g_JCCo, sJob, sCO.PadLeft(3, "0"c), msg)
        If rc = 0 Then
            MessageBox.Show("Posted.", Me.Text)
        Else
            MessageBox.Show(msg, Me.Text)
        End If
    End Sub

End Class
