-- Index lot_consumption by lot_id.
--
-- Disposal-by-lot aggregations (InventoryService._sold_proceeds_by_lot,
-- ReportingService collection-value reconstruction, LotRepository.list_for_lot
-- on void/edit) filter/group by lot_id, which had no index — every such query
-- full-scanned lot_consumption, a table that grows with every sale line forever.
CREATE INDEX IF NOT EXISTS idx_consumption_lot ON lot_consumption(lot_id);
