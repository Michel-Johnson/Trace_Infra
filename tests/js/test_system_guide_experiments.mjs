import assert from 'node:assert/strict';
import test from 'node:test';
import {
  experimentConditions,
  experimentMethods,
  runReplenishmentExperiment,
} from '../../apps/web/public/system-guide-experiments.js';

test('every policy uses one demand stream and preserves inventory, demand, and cost accounting', () => {
  for (const condition of experimentConditions) {
    const result = runReplenishmentExperiment(condition.id);
    assert.equal(result.dataset.stores, 50);
    assert.equal(result.dataset.days, 7);
    assert.equal(result.rows.length, experimentMethods.length);
    for (const row of result.rows) {
      assert.equal(row.initial_stock, result.dataset.initial_stock);
      assert.equal(row.served_units + row.stockout_units, result.dataset.total_demand);
      assert.equal(row.initial_stock + row.purchased_units,
        row.served_units + row.waste_units + row.ending_stock + row.in_transit_units);
      assert.equal(row.fill_rate, row.served_units / result.dataset.total_demand);
      assert.ok(row.fill_rate >= 0 && row.fill_rate <= 1);
      assert.equal(Math.round(row.total_cost * 100),
        Object.values(row.cost_breakdown).reduce((sum, cost) => sum + Math.round(cost * 100), 0));
      assert.equal(Math.round(row.cost_breakdown.procurement * 100),
        row.purchased_units * Math.round(result.assumptions.procurement_unit_cost * 100));
      assert.equal(Math.round(row.cost_breakdown.delivery * 100),
        row.purchased_units * Math.round(result.assumptions.delivery_unit_cost * 100));
      assert.equal(Math.round(row.cost_breakdown.transfer * 100),
        row.transferred_units * Math.round(result.assumptions.transfer_unit_cost * 100));
      assert.equal(Math.round(row.cost_breakdown.disposal * 100),
        row.waste_units * Math.round(result.assumptions.disposal_unit_cost * 100));
      assert.equal(Math.round(row.cost_breakdown.holding * 100),
        row.daily.reduce((sum, day) => sum + day.ending_stock, 0) *
          Math.round(result.assumptions.holding_unit_cost_per_day * 100));
      assert.equal(row.budget_violations, 0);
      assert.equal(row.capacity_violations, 0);
      let previousEnding = row.initial_stock;
      let previousTransit = 0;
      for (const [index, day] of row.daily.entries()) {
        assert.equal(day.day, index + 1);
        assert.equal(day.demand_units, result.dataset.demand_by_day[index]);
        assert.equal(day.opening_stock, previousEnding);
        assert.equal(day.opening_stock + day.arrived_units, day.served_units + day.waste_units + day.ending_stock);
        assert.equal(previousTransit + day.ordered_units, day.arrived_units + day.in_transit_units);
        assert.equal(day.served_units + day.stockout_units, day.demand_units);
        assert.equal(day.dispatch_units, day.ordered_units + day.transferred_units);
        assert.equal(Math.round(day.budget_spend * 100), day.ordered_units *
          Math.round((result.assumptions.procurement_unit_cost + result.assumptions.delivery_unit_cost) * 100) +
          day.transferred_units * Math.round(result.assumptions.transfer_unit_cost * 100));
        assert.ok(day.budget_spend <= result.budget);
        assert.ok(day.dispatch_units <= result.daily_dispatch_capacity);
        for (const [name, value] of Object.entries(day)) {
          assert.ok(Number.isFinite(value) && value >= 0, name);
          if (name !== 'budget_spend') assert.ok(Number.isInteger(value), name);
        }
        previousEnding = day.ending_stock;
        previousTransit = day.in_transit_units;
      }
    }
  }
});

test('repeated and interleaved experiments are deterministic and independent', () => {
  for (const condition of experimentConditions) {
    const first = runReplenishmentExperiment(condition.id);
    runReplenishmentExperiment('local-spike');
    assert.deepEqual(runReplenishmentExperiment(condition.id), first);
  }
});

test('conditions change only intended demand or arrival behavior, and methods actually differ', () => {
  const normal = runReplenishmentExperiment('normal');
  const spike = runReplenishmentExperiment('local-spike');
  const delay = runReplenishmentExperiment('delivery-delay');
  assert.deepEqual(normal.dataset, delay.dataset);
  assert.equal(normal.dataset.initial_stock, spike.dataset.initial_stock);
  for (const [index, demand] of normal.dataset.demand_by_day.entries()) {
    if (index >= 2 && index <= 4) assert.ok(spike.dataset.demand_by_day[index] > demand);
    else assert.equal(spike.dataset.demand_by_day[index], demand);
  }
  for (const [index, row] of normal.rows.entries()) {
    assert.deepEqual(row.daily.slice(0, 2), spike.rows[index].daily.slice(0, 2));
    assert.deepEqual(row.daily.slice(0, 2), delay.rows[index].daily.slice(0, 2));
    assert.ok(row.daily[2].arrived_units > 0);
    assert.equal(delay.rows[index].daily[2].arrived_units, 0);
  }
  assert.ok(normal.rows.find((row) => row.method_id === 'transfer-first').transferred_units > 0);
  assert.equal(normal.rows.find((row) => row.method_id === 'fixed-min-max').transferred_units, 0);
  assert.equal(normal.rows.find((row) => row.method_id === 'demand-priority').transferred_units, 0);
  assert.equal(new Set(normal.rows.map((row) => `${row.fill_rate}:${row.total_cost}`)).size, 3);
});

test('unsupported conditions fail explicitly', () => {
  assert.throws(() => runReplenishmentExperiment('unknown'), RangeError);
});
