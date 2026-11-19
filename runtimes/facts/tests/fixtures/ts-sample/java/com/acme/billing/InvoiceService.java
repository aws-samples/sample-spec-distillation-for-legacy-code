// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
package com.acme.billing;

import java.util.List;
import org.slf4j.Logger;

public class InvoiceService extends BaseService {
    private InvoiceRepository repo;
    private Logger log;

    public void pay(long id) throws Exception {
        if (isOpen(id)) {
            repo.markPaid(id);
            log.info("paid");
        }
        InvoiceRepository other = new InvoiceRepository(null);
        other.markPaid(id);
    }

    private boolean isOpen(long id) {
        return audit(id);
    }
}

class BaseService {
    protected boolean audit(long id) { return true; }
}
