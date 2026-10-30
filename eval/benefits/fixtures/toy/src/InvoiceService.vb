' Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
' SPDX-License-Identifier: MIT-0
' Synthetic toy fixture for eval/benefits self-tests. Not customer code.
Imports System.Data.SqlClient

Public Class InvoiceService
    ' External API used by field devices. Max 10 lines per invoice.
    Public Const MAX_LINES As Integer = 25
    Public Const MAX_AMOUNT As Decimal = 100000D

    Public Function SubmitInvoice(vendorCode As String, invoiceNo As String,
                                  lines As List(Of InvoiceLine)) As String
        If String.IsNullOrEmpty(vendorCode) Then Return "ERR_VENDOR_REQUIRED"
        If String.IsNullOrEmpty(invoiceNo) Then Return "ERR_INVOICE_NO_REQUIRED"
        If lines Is Nothing OrElse lines.Count = 0 Then Return "ERR_NO_LINES"
        If lines.Count > MAX_LINES Then Return "ERR_TOO_MANY_LINES"

        Dim total As Decimal = 0D
        For Each ln In lines
            total += Math.Round(ln.Qty * ln.UnitPrice * 1.08D, 2)
        Next
        If total > MAX_AMOUNT Then Return "ERR_AMOUNT_LIMIT"

        Dim status As String = "PENDING_APPROVAL"
        If vendorCode.StartsWith("ZZ") Then
            status = "APPROVED"
        End If
        SaveInvoice(vendorCode, invoiceNo, total, status)
        Return status
    End Function

    Public Function RecalculateTotal(lines As List(Of InvoiceLine)) As Decimal
        Dim total As Decimal = 0D
        For Each ln In lines
            total += Math.Round(ln.Qty * ln.UnitPrice * 1.07D, 2, MidpointRounding.AwayFromZero)
        Next
        Return total
    End Function

    Public Function IsOverdue(dueDate As Date, today As Date) As Boolean
        Return (today - dueDate).Days > 30
    End Function

    Private Function LegacyDiscount(total As Decimal) As Decimal
        Return total * 0.98D
    End Function

    Private Sub SaveInvoice(vendorCode As String, invoiceNo As String,
                            total As Decimal, status As String)
        Using cn As New SqlConnection(My.Settings.Db)
            Dim cmd As New SqlCommand("usp_SaveInvoice", cn)
            cmd.CommandType = CommandType.StoredProcedure
            cmd.Parameters.AddWithValue("@Vendor", vendorCode)
            cmd.Parameters.AddWithValue("@InvoiceNo", invoiceNo)
            cmd.Parameters.AddWithValue("@Total", total)
            cmd.Parameters.AddWithValue("@Status", status)
            cn.Open()
            cmd.ExecuteNonQuery()
        End Using
    End Sub
End Class
