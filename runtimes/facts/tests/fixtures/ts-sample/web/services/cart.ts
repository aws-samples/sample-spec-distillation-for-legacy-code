// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import axios from 'axios';
import { formatTotal as fmt } from './format';

export class CartService {
  constructor(private api: PriceApi) {}

  async checkout(items: string[]): Promise<string> {
    const total = await this.api.price(items);
    this.log(total);
    return fmt(total);
  }

  log(x: number): void {
    axios.post('/log', { x });
  }
}

class PriceApi {
  price(items: string[]): number { return items.length; }
}
