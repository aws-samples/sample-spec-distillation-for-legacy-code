-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- Synthetic posting procedure for the C# parser fixture. Not customer code.
CREATE PROCEDURE dbo.usp_ApInvPost
    @ApCo     tinyint,
    @ApTrans  int,
    @msg      varchar(255) OUTPUT
AS
BEGIN
    DECLARE @rcode int
    SET @rcode = 0

    DECLARE @GrossAmt money
    SELECT @GrossAmt = GrossAmt FROM dbo.ApInvHdr WHERE ApCo = @ApCo AND ApTrans = @ApTrans

    -- Business rule: an invoice over the approval threshold cannot post without approval.
    IF @GrossAmt > 5000
    BEGIN
        SET @msg = 'Invoice over the approval limit requires approval before posting.'
        SET @rcode = 1
        RETURN @rcode
    END

    UPDATE dbo.ApInvHdr SET Status = 'P' WHERE ApCo = @ApCo AND ApTrans = @ApTrans
    RETURN @rcode
END
GO
