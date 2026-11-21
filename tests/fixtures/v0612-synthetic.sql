BEGIN TRANSACTION;
CREATE TABLE action_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            approval_id TEXT,
            action_type TEXT NOT NULL,
            target TEXT NOT NULL,
            parameters_hash TEXT NOT NULL,
            outcome TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
CREATE TABLE alert_emissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_ids TEXT NOT NULL,
            emitted_at TEXT NOT NULL,
            acked_at TEXT,
            surfaced_at TEXT
        , emitted_to_gateway_at TEXT, receipt_token TEXT, channel TEXT NOT NULL DEFAULT 'script', released_at TEXT, item_versions TEXT, item_cycles TEXT, budget_reserved INTEGER NOT NULL DEFAULT 0);
CREATE TABLE approval_requests (
            id TEXT PRIMARY KEY,
            action_type TEXT NOT NULL,
            target TEXT NOT NULL,
            parameters_json TEXT NOT NULL,
            parameters_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','approved','rejected','expired','consumed')),
            requested_by TEXT NOT NULL DEFAULT '',
            approver_platform TEXT,
            approver_user_id TEXT,
            approver_chat_id TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            decided_at TEXT,
            approval_evidence TEXT NOT NULL DEFAULT '',
            consumed_at TEXT
        );
CREATE TABLE chat_source_consents (
        chat_ref TEXT PRIMARY KEY,
        lookback_days INTEGER NOT NULL DEFAULT 7 CHECK(lookback_days >= 1 AND lookback_days <= 30),
        granted_by TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        revoked_at TEXT
    );
CREATE TABLE chat_source_exclusions (
        chat_ref TEXT PRIMARY KEY,
        excluded_by TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
CREATE TABLE coordination_contacts (
        id TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        platform TEXT NOT NULL,
        handle TEXT NOT NULL,
        created_at TEXT NOT NULL,
        created_by TEXT NOT NULL DEFAULT '',
        revoked_at TEXT
    );
CREATE TABLE coordination_grants (
        id TEXT PRIMARY KEY,
        contact_id TEXT NOT NULL REFERENCES coordination_contacts(id),
        purpose TEXT NOT NULL,
        bounds TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','revoked')),
        created_at TEXT NOT NULL,
        created_by TEXT NOT NULL DEFAULT '',
        revoked_at TEXT
    );
CREATE TABLE email_body_reads (
            source_id TEXT PRIMARY KEY,
            read_at TEXT NOT NULL
        );
CREATE TABLE email_candidates (
            source_id TEXT PRIMARY KEY,
            registered_at TEXT NOT NULL
        , account_key TEXT NOT NULL DEFAULT '', policy_key TEXT NOT NULL DEFAULT '', generation INTEGER NOT NULL DEFAULT -1);
CREATE TABLE expiry_emissions (
           item_id TEXT PRIMARY KEY, emitted_at TEXT NOT NULL, expires_at TEXT NOT NULL,
           state TEXT NOT NULL DEFAULT 'reserved');
CREATE TABLE expiry_nudges (
           item_id TEXT PRIMARY KEY, fire_at TEXT NOT NULL, source_date TEXT NOT NULL,
           tier TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
           created_at TEXT NOT NULL, expires_at TEXT NOT NULL, window_end TEXT NOT NULL,
           category TEXT NOT NULL);
CREATE TABLE expiry_streaks (
           category TEXT PRIMARY KEY, declines INTEGER NOT NULL DEFAULT 0,
           last_prompt TEXT NOT NULL DEFAULT '', suggested INTEGER NOT NULL DEFAULT 0);
CREATE TABLE expiry_suppressions (
           rule_id TEXT NOT NULL, category TEXT NOT NULL, suppressed_at TEXT NOT NULL,
           PRIMARY KEY(rule_id,category));
CREATE TABLE feedback_candidates (
            id TEXT PRIMARY KEY,
            rule_text TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            status TEXT NOT NULL DEFAULT 'pending',
            fingerprint TEXT NOT NULL UNIQUE,
            occurrences INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            approved_at TEXT
        , source_ref TEXT NOT NULL DEFAULT '', decided_by TEXT NOT NULL DEFAULT '', decision_evidence TEXT NOT NULL DEFAULT '');
CREATE TABLE gmail_pending_messages (
            message_id TEXT PRIMARY KEY,
            queued_at TEXT NOT NULL
        , attempts INTEGER NOT NULL DEFAULT 0, next_retry_at TEXT, last_error_kind TEXT NOT NULL DEFAULT '', quarantined INTEGER NOT NULL DEFAULT 0 CHECK(quarantined IN (0, 1)), quarantine_cycles INTEGER NOT NULL DEFAULT 0, quarantined_at TEXT);
CREATE TABLE gmail_unreadable (
        message_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        attempts INTEGER NOT NULL,
        first_failed_at TEXT NOT NULL,
        last_failed_at TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT ''
    );
CREATE TABLE hermes_tingle_bookings (
        item_id TEXT NOT NULL REFERENCES hermes_tingle_items(id),
        position INTEGER NOT NULL CHECK(position > 0),
        kind TEXT NOT NULL DEFAULT 'other'
            CHECK(kind IN ('flight', 'train', 'lodging', 'rental', 'event', 'other')),
        name TEXT NOT NULL,
        detail TEXT DEFAULT '',
        state TEXT NOT NULL DEFAULT 'researched'
            CHECK(state IN ('researched', 'booked', 'confirmed', 'cancelled')),
        cancel_by TEXT,
        cancel_item_id TEXT,
        confirmation_ref TEXT DEFAULT '',
        note TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(item_id, position)
    );
CREATE TABLE hermes_tingle_documents (
        item_id TEXT NOT NULL REFERENCES hermes_tingle_items(id),
        position INTEGER NOT NULL CHECK(position > 0),
        name TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'needed'
            CHECK(state IN ('needed', 'collected', 'submitted', 'accepted', 'rejected', 'expired')),
        valid_until TEXT,
        note TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(item_id, position)
    );
CREATE TABLE hermes_tingle_items (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            owner TEXT NOT NULL DEFAULT 'me',
            priority TEXT NOT NULL DEFAULT 'normal',
            source_type TEXT NOT NULL DEFAULT 'conversation',
            source_ref TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            completion_signal TEXT NOT NULL DEFAULT '',
            reply_interval_seconds INTEGER,
            due_at TEXT,
            next_check_at TEXT,
            recur_interval INTEGER CHECK(recur_interval > 0),
            recur_unit TEXT CHECK(recur_unit IN ('day','month','year')),
            recur_lead_days INTEGER CHECK(recur_lead_days >= 0),
            recur_anchor TEXT,
            snoozed_until TEXT,
            reminder_count INTEGER NOT NULL DEFAULT 0,
            last_notified_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        , topic_emoji TEXT NOT NULL DEFAULT '📌', origin_platform TEXT, origin_chat_id TEXT, origin_message_id TEXT, reminder_revision INTEGER NOT NULL DEFAULT 0, reminder_cycle TEXT NOT NULL DEFAULT '', hard_deadline INTEGER NOT NULL DEFAULT 0, watch_ref TEXT, watch_state TEXT, watch_updated_at TEXT, watch_max_age_hours INTEGER CHECK(watch_max_age_hours > 0), reminder_attempt_count INTEGER NOT NULL DEFAULT 0, reminder_budget_uncertain INTEGER NOT NULL DEFAULT 0);
INSERT INTO "hermes_tingle_items" VALUES('HT-69C00E','Synthetic preserved predecessor','Synthetic detail','open','me','normal','conversation','','','',NULL,'2027-01-04T12:00:00+00:00','2027-01-03T12:00:00+00:00',NULL,NULL,NULL,NULL,NULL,0,NULL,'2026-11-18T22:10:26+00:00','2026-11-18T22:10:26+00:00',NULL,'📌',NULL,NULL,NULL,0,'81a100ca4c6b4788e2e57d093418ee5d',1,NULL,NULL,NULL,NULL,0,0);
CREATE TABLE hermes_tingle_options (
        item_id TEXT NOT NULL REFERENCES hermes_tingle_items(id),
        position INTEGER NOT NULL CHECK(position > 0),
        name TEXT NOT NULL,
        detail TEXT DEFAULT '',
        price_text TEXT DEFAULT '',
        state TEXT NOT NULL DEFAULT 'candidate'
            CHECK(state IN ('candidate', 'shortlisted', 'rejected', 'proposed')),
        note TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(item_id, position)
    );
CREATE TABLE hermes_tingle_requirements (
        item_id TEXT NOT NULL REFERENCES hermes_tingle_items(id),
        position INTEGER NOT NULL CHECK(position > 0),
        text TEXT NOT NULL,
        must INTEGER NOT NULL DEFAULT 0 CHECK(must IN (0, 1)),
        met INTEGER NOT NULL DEFAULT 0 CHECK(met IN (0, 1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(item_id, position)
    );
CREATE TABLE hermes_tingle_source_optins (
        kind TEXT PRIMARY KEY CHECK(kind IN ('email','calendar','document','chat','meeting')),
        enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
        lookback_days INTEGER CHECK(lookback_days IS NULL OR (lookback_days >= 1 AND lookback_days <= 30)),
        enabled_by TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        enabled_at TEXT,
        disabled_at TEXT
    , generation INTEGER NOT NULL DEFAULT 0, calendar_context INTEGER CHECK(calendar_context IN (0,1)));
INSERT INTO "hermes_tingle_source_optins" VALUES('email',0,NULL,'','',NULL,NULL,0,NULL);
INSERT INTO "hermes_tingle_source_optins" VALUES('calendar',1,NULL,'Synthetic owner','','2026-11-18T22:10:26+00:00',NULL,1,0);
INSERT INTO "hermes_tingle_source_optins" VALUES('chat',0,NULL,'','',NULL,NULL,0,NULL);
INSERT INTO "hermes_tingle_source_optins" VALUES('meeting',0,NULL,'','',NULL,NULL,0,NULL);
INSERT INTO "hermes_tingle_source_optins" VALUES('document',0,NULL,'','',NULL,NULL,0,NULL);
CREATE TABLE hermes_tingle_steps (
        goal_id TEXT NOT NULL REFERENCES hermes_tingle_items(id),
        position INTEGER NOT NULL CHECK(position > 0),
        title TEXT NOT NULL,
        why TEXT NOT NULL,
        done INTEGER NOT NULL DEFAULT 0 CHECK(done IN (0, 1)),
        skipped INTEGER NOT NULL DEFAULT 0 CHECK(skipped IN (0, 1)),
        wait_item_id TEXT UNIQUE REFERENCES hermes_tingle_items(id),
        PRIMARY KEY(goal_id, position)
    );
CREATE TABLE inbound_messages (
        id TEXT PRIMARY KEY,
        contact_id TEXT NOT NULL,
        platform TEXT NOT NULL,
        handle TEXT NOT NULL,
        body TEXT NOT NULL,
        received_at TEXT NOT NULL,
        reviewed_at TEXT
    );
CREATE TABLE known_chats (
        chat_ref TEXT PRIMARY KEY,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        origin TEXT NOT NULL DEFAULT ''
    );
CREATE TABLE outbound_messages (
        id TEXT PRIMARY KEY,
        contact_id TEXT NOT NULL REFERENCES coordination_contacts(id),
        approval_id TEXT NOT NULL,
        purpose TEXT NOT NULL,
        words TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'prepared' CHECK(status IN ('prepared','sent','failed','cancelled','outcome_unknown')),
        created_at TEXT NOT NULL,
        sent_at TEXT,
        error TEXT NOT NULL DEFAULT '',
        reply_consumed_by TEXT
    );
CREATE TABLE plugin_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
INSERT INTO "plugin_meta" VALUES('reminder_cycle_provenance_v1','1');
INSERT INTO "plugin_meta" VALUES('missed_schedule_recovery_v2','1');
INSERT INTO "plugin_meta" VALUES('synthetic_custom','preserve');
CREATE TABLE recurrence_completions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL,
            occurrence_due_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT ''
        );
CREATE TABLE source_seen (
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            source_version TEXT NOT NULL DEFAULT '',
            reviewed_at TEXT NOT NULL,
            PRIMARY KEY (source_type, source_id, source_version)
        );
CREATE UNIQUE INDEX idx_hermes_tingle_items_source_ref
            ON hermes_tingle_items(source_type, source_ref) WHERE source_ref <> '';
CREATE INDEX idx_hermes_tingle_items_due
            ON hermes_tingle_items(status, next_check_at);
CREATE INDEX idx_feedback_candidates_status
            ON feedback_candidates(status, created_at);
CREATE INDEX idx_approval_requests_status
            ON approval_requests(status, created_at);
CREATE TRIGGER reminder_cycle_insert AFTER INSERT ON hermes_tingle_items WHEN NEW.reminder_cycle='' BEGIN UPDATE hermes_tingle_items SET reminder_cycle=lower(hex(randomblob(16))) WHERE id=NEW.id; END;
CREATE INDEX idx_reminder_cycle ON hermes_tingle_items(reminder_cycle);
CREATE TRIGGER reminder_linked_wait_update
        AFTER UPDATE OF title,summary,status,owner,snoozed_until,completion_signal
        ON hermes_tingle_items BEGIN
        UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1
        WHERE id IN (SELECT goal_id FROM hermes_tingle_steps WHERE wait_item_id=NEW.id);
        END;
CREATE TRIGGER reminder_steps_insert AFTER INSERT ON hermes_tingle_steps BEGIN UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1 WHERE id=NEW.goal_id; END;
CREATE TRIGGER reminder_steps_update AFTER UPDATE ON hermes_tingle_steps BEGIN UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1 WHERE id=NEW.goal_id; END;
CREATE TRIGGER reminder_steps_delete AFTER DELETE ON hermes_tingle_steps BEGIN UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1 WHERE id=OLD.goal_id; END;
CREATE TRIGGER reminder_hermes_tingle_documents_insert AFTER INSERT ON hermes_tingle_documents BEGIN UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1 WHERE id=NEW.item_id; END;
CREATE TRIGGER reminder_hermes_tingle_documents_update AFTER UPDATE ON hermes_tingle_documents BEGIN UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1 WHERE id=NEW.item_id; END;
CREATE TRIGGER reminder_hermes_tingle_documents_delete AFTER DELETE ON hermes_tingle_documents BEGIN UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1 WHERE id=OLD.item_id; END;
CREATE TRIGGER reminder_hermes_tingle_requirements_insert AFTER INSERT ON hermes_tingle_requirements BEGIN UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1 WHERE id=NEW.item_id; END;
CREATE TRIGGER reminder_hermes_tingle_requirements_update AFTER UPDATE ON hermes_tingle_requirements BEGIN UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1 WHERE id=NEW.item_id; END;
CREATE TRIGGER reminder_hermes_tingle_requirements_delete AFTER DELETE ON hermes_tingle_requirements BEGIN UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1 WHERE id=OLD.item_id; END;
CREATE TRIGGER reminder_hermes_tingle_options_insert AFTER INSERT ON hermes_tingle_options BEGIN UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1 WHERE id=NEW.item_id; END;
CREATE TRIGGER reminder_hermes_tingle_options_update AFTER UPDATE ON hermes_tingle_options BEGIN UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1 WHERE id=NEW.item_id; END;
CREATE TRIGGER reminder_hermes_tingle_options_delete AFTER DELETE ON hermes_tingle_options BEGIN UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1 WHERE id=OLD.item_id; END;
CREATE TRIGGER expiry_item_changed AFTER UPDATE OF status,due_at,completion_signal,snoozed_until ON hermes_tingle_items
           BEGIN DELETE FROM expiry_nudges WHERE item_id=NEW.id AND
           (NEW.status NOT IN ('open','waiting','blocked','missed') OR NEW.due_at IS NOT NULL
            OR NEW.completion_signal='source_email_reply_received' OR
            (NEW.snoozed_until IS NOT NULL AND NEW.snoozed_until>expiry_nudges.fire_at)); END;
CREATE TRIGGER expiry_item_deleted AFTER DELETE ON hermes_tingle_items
           BEGIN DELETE FROM expiry_nudges WHERE item_id=OLD.id;
                 DELETE FROM expiry_emissions WHERE item_id=OLD.id; END;
DELETE FROM "sqlite_sequence";
COMMIT;
