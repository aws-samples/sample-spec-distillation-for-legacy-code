' Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
' SPDX-License-Identifier: MIT-0
' Synthetic fixture for eval/keygen tests. Not real code.
Imports System.ComponentModel.DataAnnotations

Public Module modBilling
    Public Const CREDIT_LIMIT As Decimal = 25000D

    Public Enum InvoiceState
        Open
        Approved
        Paid
    End Enum

    Public Function CheckCredit(ByVal amount As Decimal) As Boolean
        If amount > CREDIT_LIMIT Then Throw New InvalidOperationException("Credit limit exceeded")
        Return True
    End Function
End Module

Public Class InvoiceModel
    <Required>
    <StringLength(20)>
    Public Property InvoiceNo As String
End Class
