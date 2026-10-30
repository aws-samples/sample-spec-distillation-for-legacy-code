' Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
' SPDX-License-Identifier: MIT-0
' Synthetic fixture for identifier_evidence tests (no customer code).
Public Class clsShipDAL

    Public Function GetUntransmitted() As DataTable
        Return FillTable("SELECT * FROM ShipHdr WHERE XmitFlg='N' ORDER BY ShipNo")
    End Function

    Public Function MarkTransmitted(ByVal ShipNo As Integer) As Boolean
        Return ExecSql("UPDATE ShipHdr SET XmitFlg='Y', XmitDate=GETDATE() WHERE ShipNo=@n")
    End Function

    Public Function SaveHazard(ByVal ShipNo As Integer, ByVal HzdCls As String) As Boolean
        Return ExecSql("UPDATE ShipHdr SET HzdCls=@h WHERE ShipNo=@n")
    End Function

End Class
