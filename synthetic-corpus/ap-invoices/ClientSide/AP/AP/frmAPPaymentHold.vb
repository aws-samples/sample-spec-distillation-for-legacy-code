' Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
'==============================================================================
' frmAPPaymentHold.vb
' Payment holds - add manual holds, release holds, check pay eligibility.
'   06/2013 PSV  created (replaces the HoldYN checkbox on entry)
'   04/2017 PSV  RET hold release, job must be complete
'==============================================================================
Option Strict Off
Option Explicit On

Imports System.Data
Imports System.Data.SqlClient
Imports System.Collections.Generic
Imports System.Windows.Forms

Public Class frmAPPaymentHold
    Inherits System.Windows.Forms.Form

    Friend WithEvents txtMth As New TextBox
    Friend WithEvents txtAPTrans As New TextBox
    Friend WithEvents btnLookup As New Button
    Friend WithEvents grdHolds As New DataGridView
    Friend WithEvents cboHoldCode As New ComboBox
    Friend WithEvents txtNotes As New TextBox
    Friend WithEvents btnAddHold As New Button
    Friend WithEvents btnRelease As New Button
    Friend WithEvents btnCanPay As New Button
    Friend WithEvents lblInvInfo As New Label

    Private m_BL As clsAPInvoiceBL
    Private m_Hdr As DataRow
    Private m_dtHolds As DataTable

    Private Sub frmAPPaymentHold_Load(ByVal sender As Object, ByVal e As System.EventArgs) Handles MyBase.Load
        m_BL = New clsAPInvoiceBL
        Me.Text = "AP Payment Holds"
        ' only DSP and MGR can be put on by hand. LW / INS come from approval,
        ' RET is put on by the retainage process
        cboHoldCode.Items.Clear()
        cboHoldCode.Items.Add(HOLD_DISPUTE)
        If g_UserRole <> ROLE_CLERK Then cboHoldCode.Items.Add(HOLD_MGR)
        btnAddHold.Enabled = False
        btnRelease.Enabled = False
    End Sub

    Private Sub btnLookup_Click(ByVal sender As Object, ByVal e As System.EventArgs) Handles btnLookup.Click
        Dim mth As Date
        If Not Date.TryParse(txtMth.Text, mth) Then
            MsgBox("Enter month as MM/yyyy", MsgBoxStyle.Exclamation)
            Exit Sub
        End If
        mth = FirstOfMonth(mth)
        m_Hdr = m_BL.DAL.GetInvoice(g_APCo, mth, CInt(Val(txtAPTrans.Text)))
        If m_Hdr Is Nothing Then
            lblInvInfo.Text = "Not found."
            Exit Sub
        End If
        lblInvInfo.Text = m_Hdr("APRef").ToString() & "  " & Format(CDec(m_Hdr("GrossAmt")), "$#,##0.00") & _
                          "  " & StatusText(m_Hdr("Status").ToString())
        RefreshHolds()
    End Sub

    Private Sub RefreshHolds()
        m_dtHolds = m_BL.DAL.GetOpenHolds(CByte(m_Hdr("APCo")), CDate(m_Hdr("Mth")), CInt(m_Hdr("APTrans")))
        grdHolds.DataSource = m_dtHolds
        Dim st As String = m_Hdr("Status").ToString()
        ' holds can only go on approved (or already held) invoices
        btnAddHold.Enabled = (st = STAT_APPROVED OrElse st = STAT_HOLD)
        btnRelease.Enabled = m_dtHolds.Rows.Count > 0
        For Each gr As DataGridViewRow In grdHolds.Rows
            If gr.DataBoundItem Is Nothing Then Continue For
            Dim hr As DataRow = CType(gr.DataBoundItem, DataRowView).Row
            If Not m_BL.CanReleaseHold(g_UserRole, hr("HoldCode").ToString(), hr("RelRole").ToString(), _
                                       hr("HoldBy").ToString()) Then
                gr.DefaultCellStyle.ForeColor = Drawing.Color.Gray
            End If
        Next
    End Sub

    Private Sub btnAddHold_Click(ByVal sender As Object, ByVal e As System.EventArgs) Handles btnAddHold.Click
        If m_Hdr Is Nothing OrElse cboHoldCode.SelectedItem Is Nothing Then Exit Sub
        Dim sCode As String = cboHoldCode.SelectedItem.ToString()
        If sCode = HOLD_DISPUTE AndAlso Trim(txtNotes.Text) = "" Then
            MsgBox("Disputed holds need a note describing the dispute.", MsgBoxStyle.Exclamation)
            Exit Sub
        End If
        If m_BL.DAL.AddHold(CByte(m_Hdr("APCo")), CDate(m_Hdr("Mth")), CInt(m_Hdr("APTrans")), sCode, Trim(txtNotes.Text)) Then
            m_Hdr = m_BL.DAL.GetInvoice(CByte(m_Hdr("APCo")), CDate(m_Hdr("Mth")), CInt(m_Hdr("APTrans")))
            RefreshHolds()
        Else
            MsgBox("Add hold failed: " & m_BL.DAL.LastError, MsgBoxStyle.Critical)
        End If
    End Sub

    Private Sub btnRelease_Click(ByVal sender As Object, ByVal e As System.EventArgs) Handles btnRelease.Click
        If grdHolds.CurrentRow Is Nothing OrElse grdHolds.CurrentRow.DataBoundItem Is Nothing Then Exit Sub
        Dim hr As DataRow = CType(grdHolds.CurrentRow.DataBoundItem, DataRowView).Row
        Dim sCode As String = hr("HoldCode").ToString()

        If Not m_BL.CanReleaseHold(g_UserRole, sCode, hr("RelRole").ToString(), hr("HoldBy").ToString()) Then
            MsgBox("You are not allowed to release a " & sCode & " hold.", MsgBoxStyle.Exclamation)
            Exit Sub
        End If

        If sCode = HOLD_LIENWVR Then
            If MsgBox("Confirm the signed lien waiver has been received and filed?", _
                      MsgBoxStyle.YesNo Or MsgBoxStyle.Question) = MsgBoxResult.No Then Exit Sub
        End If

        Dim sMsg As String = ""
        Dim rc As Integer = m_BL.DAL.ReleaseHold(CByte(m_Hdr("APCo")), CDate(m_Hdr("Mth")), CInt(m_Hdr("APTrans")), _
                                                 sCode, Trim(txtNotes.Text), sMsg)
        If rc <> 0 Then
            MsgBox("Release failed (" & rc & "): " & sMsg, MsgBoxStyle.Critical)
        End If
        m_Hdr = m_BL.DAL.GetInvoice(CByte(m_Hdr("APCo")), CDate(m_Hdr("Mth")), CInt(m_Hdr("APTrans")))
        RefreshHolds()
    End Sub

    Private Sub btnCanPay_Click(ByVal sender As Object, ByVal e As System.EventArgs) Handles btnCanPay.Click
        If m_Hdr Is Nothing Then Exit Sub
        Dim reasons As List(Of String) = Nothing
        If m_BL.CanPay(m_Hdr, reasons) Then
            MsgBox("Invoice is eligible for payment.", MsgBoxStyle.Information)
        Else
            MsgBox("Cannot pay:" & vbCrLf & String.Join(vbCrLf, reasons.ToArray()), MsgBoxStyle.Exclamation)
        End If
    End Sub

End Class
