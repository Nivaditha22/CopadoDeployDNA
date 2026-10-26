# DeployDNA — Setup & Run Guide

> **Forensic deployment auditor for Salesforce releases.**  
> Prevents catastrophic deployments by detecting historical failure DNA matches and in-flight component collisions before promotion.

---

## Table of Contents

1. [What This App Does](#1-what-this-app-does)
2. [Architecture at a Glance](#2-architecture-at-a-glance)
3. [Prerequisites](#3-prerequisites)
4. [Installation](#4-installation)
5. [Configuration (.env)](#5-configuration-env)
6. [Running DeployDNA Locally](#6-running-deploydna-locally)
7. [Exposing the App with Cloudflare Tunnel](#7-exposing-the-app-with-cloudflare-tunnel)
8. [Wiring Salesforce to Your Tunnel](#8-wiring-salesforce-to-your-tunnel)
9. [Using the App](#9-using-the-app)
10. [Adding Test Data](#10-adding-test-data)
    - [A. Anonymous Apex Window — Create Demo Stories](#a-anonymous-apex-window--create-demo-stories)
    - [B. Apex Trigger — The "Bad Trigger" for the Demo](#b-apex-trigger--the-bad-trigger-for-the-demo)
    - [C. Validation Rules — Block Promotion](#c-validation-rules--block-promotion)
11. [Troubleshooting](#11-troubleshooting)
12. [Quick Reference](#12-quick-reference)

---

## 1. What This App Does

DeployDNA audits a Copado **User Story** before deployment and:

- Reads the story's **Metadata Components** (its "DNA") directly from the org.
- Scans for **in-flight collisions** — other active stories touching the same components.
- Scans the **historical failure corpus** — cancelled stories that share components (the "corporate amnesia" cure).
- Uses **Copado Agentia** to reason over the evidence and assign a risk verdict: `HIGH`, `MEDIUM`, `LOW`, `CLEAR`, or `INCOMPLETE`.
- **Writes back** the verdict to `Risk_Score__c` on the story and attaches a branded **PDF audit report**.
- Fails **closed** — a broken audit becomes `INCOMPLETE`, never `CLEAR`.

There are three ways to invoke it:

| Interface | Use case |
|---|---|
| **CLI** (`deploydna.py`) | Local runs, CI/CD gating, manual audits |
| **Flask Server** (`deploydna_server.py`) | Long-running service, webhook target, REST API |
| **Salesforce Webhook** (Apex) | Triggered from a Flow, LWC, or status change |

---

## 2. Architecture at a Glance

```
┌──────────────────┐     webhook      ┌────────────────────────┐
│  Salesforce      │ ───────────────► │  Flask Server          │
│  (Flow / LWC /   │  POST /webhook/  │  (localhost:5000)      │
│   Apex callout)  │  deploydna       │                        │
└──────────────────┘                  └──────────┬─────────────┘
                                                 │
                                    ┌────────────┴────────────┐
                                    │  deploydna.py           │
                                    │  ─ read org (SF CLI)    │
                                    │  ─ reason (Agentia)     │
                                    │  ─ write Risk_Score__c  │
                                    │  ─ attach PDF           │
                                    └────────────┬────────────┘
                                                 │
                                                 ▼
                                      ┌──────────────────┐
                                      │  Copado Org      │
                                      │  (Risk_Score__c, │
                                      │   PDF attached)  │
                                      └──────────────────┘
```

The Flask server is what you expose via Cloudflare Tunnel — Salesforce can't call `localhost` directly.

---

## 3. Prerequisites

Install all of the following on the machine that will run the app:

| Requirement | Why | Verify |
|---|---|---|
| **Python 3.9+** | Runs the app | `python3 --version` |
| **Salesforce CLI (`sf`)** | Reads/writes the org, uploads PDFs | `sf --version` |
| **Cloudflare Tunnel (`cloudflared`)** | Exposes localhost to Salesforce | `cloudflared --version` |
| **A Copado-connected Salesforce org** | Target of the audit | — |
| **Copado Agentia credentials** | PAT, Org ID, Workspace ID | — |

Install the Salesforce CLI:
- **macOS:** `brew install salesforce-cli`
- **Windows:** Download from https://developer.salesforce.com/tools/salesforcecli
- **Linux:** See https://developer.salesforce.com/docs/atlas.en-us.sfdx_setup.meta/sfdx_setup/sfdx_setup_install_cli.htm

Install Cloudflare Tunnel:
- **macOS:** `brew install cloudflared`
- **Windows:** `winget install --id Cloudflare.cloudflared`
- **Linux:** See https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/

Authenticate the SF CLI against your Copado org **once**:
```bash
sf org login web --alias copado-trial
```
The alias you choose here is what you'll put in `.env` as `SF_TARGET_ORG` and pass to `--org`.

---

## 4. Installation

```bash
# 1. Clone or copy the project files
cd /path/to/deploydna

# 2. Create a virtual environment (recommended)
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install Python dependencies
pip install fpdf2 requests python-dotenv flask flask-cors

# 4. (Optional) Pin them
pip freeze > requirements.txt
```

The dependency list, if you prefer a `requirements.txt`:

```txt
fpdf2>=2.7.0
requests>=2.31.0
python-dotenv>=1.0.0
flask>=3.0.0
flask-cors>=4.0.0
```

---

## 5. Configuration (.env)

Create a `.env` file in the project root:

```env
# ---------- Salesforce ----------
SF_TARGET_ORG=copado-trial

# ---------- Copado Agentia ----------
COPADO_PAT=your-personal-access-token
COPADO_ORG_ID=your-copado-org-id
COPADO_WORKSPACE_ID=your-workspace-id
COPADO_API_BASE=https://copadogpt-api.robotic.copado.com

# ---------- Server ----------
PORT=5000
LOOKBACK_DAYS=365
DEPLOYDNA_COOLDOWN=60
DEPLOYDNA_EVIDENCE=auto       # auto | agent | soql

# ---------- Optional debug ----------
# DEPLOYDNA_DEBUG=1
```

### What each variable does

| Variable | Description |
|---|---|
| `SF_TARGET_ORG` | Default SF CLI org alias (must exist via `sf org login web --alias ...`) |
| `COPADO_PAT` | Copado Personal Access Token |
| `COPADO_ORG_ID` | Copado organization identifier |
| `COPADO_WORKSPACE_ID` | Copado workspace identifier (where Agentia runs) |
| `COPADO_API_BASE` | Agentia API base URL |
| `PORT` | Flask port (default `5000`) |
| `LOOKBACK_DAYS` | Days of cancelled-story history to scan (default `365`) |
| `DEPLOYDNA_COOLDOWN` | Seconds to ignore duplicate webhook triggers for the same story |
| `DEPLOYDNA_EVIDENCE` | `auto`: Agentia reads org, falls back to SOQL. `agent`: Agentia must read org. `soql`: Python reads org, Agentia only reasons. |

> **Tip:** If Agentia has no org-read tools available in your workspace, set `DEPLOYDNA_EVIDENCE=soql`. Python reads the org via `sf` CLI and Agentia only reasons over the supplied evidence — no org-read tools required in Agentia.

---

## 6. Running DeployDNA Locally

### 6.1 CLI — one-off audit

```bash
# Basic audit (writes Risk_Score__c, attaches PDF)
python deploydna.py --story US-0000123 --org copado-trial --writeback

# JSON output (good for CI)
python deploydna.py --story US-0000123 --org copado-trial --json

# CI gate — exit code 2 = HIGH risk, 3 = INCOMPLETE
python deploydna.py --story US-0000123 --org copado-trial --gate

# Save the PDF locally too
python deploydna.py --story US-0000123 --org copado-trial --pdf ./report.pdf

# List recent demo stories
python deploydna.py --list-demos --org copado-trial
```

| Flag | Effect |
|---|---|
| `--story NAME` | **Required.** The user story to audit (e.g. `US-0000123`) |
| `--org ALIAS` | SF CLI alias (default `copado-trial`) |
| `--writeback` | Writes `Risk_Score__c` + attaches PDF |
| `--json` | Prints report JSON to stdout |
| `--gate` | Exits `2` on HIGH, `3` on INCOMPLETE |
| `--pdf FILE` | Saves PDF locally |
| `--mock` | Uses local mock data (no Agentia call) |
| `--lookback N` | Days of history (default `365`) |
| `--evidence {auto,agent,soql}` | Who reads the org |
| `--list-demos` | Lists 25 recent user stories |

### 6.2 Flask server

```bash
python deploydna_server.py
```

You should see:

```
====================================================
  DeployDNA Headless Server : http://127.0.0.1:5000
  Webhook Endpoint          : POST /webhook/deploydna
  Default Salesforce Org    : copado-trial
  Evidence mode             : auto
====================================================
```

### 6.3 Server endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | Health check + currently-running stories |
| `/webhook/deploydna` | POST / GET | Async webhook (returns `202` immediately, audit runs in background) |
| `/api/audit` | GET | Synchronous audit (blocks until verdict is written) |

---

## 7. Exposing the App with Cloudflare Tunnel

Salesforce can't reach `http://localhost:5000`, so we use Cloudflare Tunnel to give it a public HTTPS URL.

### 7.1 Start the Flask server first

```bash
python deploydna_server.py
```

Leave this running in one terminal.

### 7.2 Start the tunnel in a second terminal

```bash
cloudflared tunnel --url http://localhost:5000
```

You'll see output like:

```
2026-10-14T10:00:00Z INF Requesting new quick Tunnel on trycloudflare.com...
2026-10-14T10:00:05Z INF Your quick Tunnel has been created!
2026-10-14T10:00:05Z INF https://appreciate-varying-systems-chicago.trycloudflare.com
```

**Copy that URL.** This is your tunnel URL.

> ⚠️ **Important:** Quick tunnels generate a **new URL every time you restart `cloudflared`**. If you restart it, you must update the Salesforce Remote Site Setting **and** the Apex class with the new URL.
>
> If you need a stable URL, use a **named tunnel** (`cloudflared tunnel create deploydna` + DNS record) — see https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/

### 7.3 Verify the tunnel works

From any machine:

```bash
curl https://appreciate-varying-systems-chicago.trycloudflare.com/health
```

Expected response:

```json
{"status":"ok","org":"copado-trial","running":[]}
```

---

## 8. Wiring Salesforce to Your Tunnel

Two things must be updated in Salesforce whenever the tunnel URL changes.

### 8.1 Add the tunnel domain to Remote Site Settings

**Setup → Security → Remote Site Settings → New Remote Site**

| Field | Value |
|---|---|
| Remote Site Name | `DeployDNA_Tunnel` |
| Remote Site URL | `https://appreciate-varying-systems-chicago.trycloudflare.com` (no trailing slash, no path) |
| Active | ✅ |

> If you change the tunnel URL later, edit this record — don't create a new one.

### 8.2 Update the Apex webhook class

Open the Apex class **`DeployDNAWebhook`** in Setup → Apex Classes (or via the Developer Console) and find the `makeCallout` method:

```apex
@future(callout=true)
private static void makeCallout(String storyName, String orgAlias) {
    HttpRequest req = new HttpRequest();
    req.setEndpoint('https://appreciate-varying-systems-chicago.trycloudflare.com/webhook/deploydna');
    req.setMethod('POST');
    req.setHeader('Content-Type', 'application/json');
    ...
}
```

Replace **only** the `setEndpoint` URL with your current tunnel URL + `/webhook/deploydna`. Then save and deploy.

**Full class reference** (if you're creating it fresh — this is the file `DeployDNAWebhook.cls`):

```apex
public without sharing class DeployDNAWebhook {
    public static Set<String> processedStories = new Set<String>();

    public class WebhookRequest {
        @InvocableVariable(required=true label='User Story Name')
        public String storyName;
        @InvocableVariable(required=false label='Target Org Alias')
        public String orgAlias;
    }

    public class AuditStateWrapper {
        @AuraEnabled public String storyName;
        @AuraEnabled public String riskScore;
        @AuraEnabled public Integer attachmentCount;
        @AuraEnabled public String latestFileId;
    }

    @InvocableMethod(label='Trigger DeployDNA Audit' description='Sets ANALYZING state and sends US to Cloudflare Tunnel')
    public static void sendToDeployDNA(List<WebhookRequest> requests) {
        List<String> storyNames = new List<String>();
        String aliasToUse = 'copado-trial';

        for (WebhookRequest req : requests) {
            if (req != null && String.isNotBlank(req.storyName) && !processedStories.contains(req.storyName)) {
                processedStories.add(req.storyName);
                storyNames.add(req.storyName.trim());
                if (String.isNotBlank(req.orgAlias)) {
                    aliasToUse = req.orgAlias.trim();
                }
            }
        }

        if (!storyNames.isEmpty()) {
            List<copado__User_Story__c> storiesToMark = [
                SELECT Id, Name, Risk_Score__c
                FROM copado__User_Story__c
                WHERE Name IN :storyNames
            ];
            for (copado__User_Story__c us : storiesToMark) {
                us.Risk_Score__c = 'ANALYZING';
            }
            update storiesToMark;

            for (String sName : storyNames) {
                makeCallout(sName, aliasToUse);
            }
        }
    }

    @AuraEnabled
    public static void triggerFromLwc(Id storyId) {
        copado__User_Story__c us = [SELECT Id, Name, Risk_Score__c FROM copado__User_Story__c WHERE Id = :storyId LIMIT 1];
        us.Risk_Score__c = 'ANALYZING';
        update us;
        makeCallout(us.Name, 'copado-trial');
    }

    @AuraEnabled
    public static AuditStateWrapper getStoryAuditState(Id storyId) {
        copado__User_Story__c us = [
            SELECT Id, Name, Risk_Score__c
            FROM copado__User_Story__c
            WHERE Id = :storyId
            LIMIT 1
        ];

        List<ContentDocumentLink> files = [
            SELECT ContentDocumentId, ContentDocument.Title, ContentDocument.FileExtension
            FROM ContentDocumentLink
            WHERE LinkedEntityId = :storyId
            AND ContentDocument.FileExtension = 'pdf'
            ORDER BY SystemModstamp DESC
            LIMIT 1
        ];

        List<Attachment> atts = [
            SELECT Id
            FROM Attachment
            WHERE ParentId = :storyId
            ORDER BY CreatedDate DESC
            LIMIT 1
        ];

        AuditStateWrapper state = new AuditStateWrapper();
        state.storyName = us.Name;
        state.riskScore = us.Risk_Score__c;

        if (!files.isEmpty()) {
            state.latestFileId = files[0].ContentDocumentId;
        } else if (!atts.isEmpty()) {
            state.latestFileId = atts[0].Id;
        } else {
            state.latestFileId = null;
        }

        Integer fileCount = [SELECT COUNT() FROM ContentDocumentLink WHERE LinkedEntityId = :storyId];
        Integer attCount = [SELECT COUNT() FROM Attachment WHERE ParentId = :storyId];
        state.attachmentCount = fileCount + attCount;

        return state;
    }

    @future(callout=true)
    private static void makeCallout(String storyName, String orgAlias) {
        HttpRequest req = new HttpRequest();
        // ▼▼▼ REPLACE THIS URL WITH YOUR CURRENT TUNNEL ▼▼▼
        req.setEndpoint('https://appreciate-varying-systems-chicago.trycloudflare.com/webhook/deploydna');
        req.setMethod('POST');
        req.setHeader('Content-Type', 'application/json');

        String finalOrg = String.isNotBlank(orgAlias) ? orgAlias.trim() : 'copado-trial';
        Map<String, String> payload = new Map<String, String>{
            'storyName' => storyName.trim(),
            'orgAlias'  => finalOrg
        };
        req.setBody(JSON.serialize(payload));

        try {
            new Http().send(req);
        } catch (Exception e) {
            System.debug('DeployDNA Callout Error: ' + e.getMessage());
        }
    }
}
```

### 8.3 End-to-end sanity check

1. Flask server is running → terminal shows `http://127.0.0.1:5000`.
2. `cloudflared` is running → terminal shows the tunnel URL.
3. `curl https://<your-tunnel>.trycloudflare.com/health` returns `{"status":"ok",...}`.
4. Remote Site Setting is `Active` and its URL matches the tunnel domain (host only).
5. Apex class `setEndpoint` includes the full `https://.../webhook/deploydna` path.

When all five are true, run a test in Anonymous Apex:

```apex
DeployDNAWebhook.triggerFromLwc('a01XXXXXXXXXXXXXXX');
```

(Replace with a real User Story Id.) The Flask terminal should log `[*] [AUDIT START]`.

---

## 9. Using the App

### 9.1 CLI (fastest way to test)

```bash
python deploydna.py --story US-0000123 --org copado-trial --writeback --pdf report.pdf
```

### 9.2 Server — synchronous REST API

```bash
curl "http://localhost:5000/api/audit?story=US-0000123&org=copado-trial&writeback=1"
```

### 9.3 Server — async webhook (what Salesforce uses)

```bash
curl -X POST http://localhost:5000/webhook/deploydna \
  -H "Content-Type: application/json" \
  -d '{"storyName":"US-0000123","orgAlias":"copado-trial"}'
```

Returns immediately:

```json
{"status":"queued","story":"US-0000123","org":"copado-trial","message":"DeployDNA background audit started for US-0000123"}
```

The audit then runs in the background. Poll `Risk_Score__c` on the story to see the verdict, or query the LWC's `getStoryAuditState`.

### 9.4 Server — health & in-flight

```bash
curl http://localhost:5000/health
```

---

## 10. Adding Test Data

The following creates a complete "high risk" demo scenario:
- **The Ghost** — a cancelled story sharing a component (represents the historical failure).
- **The Collision** — an active story touching the same component (the parallel developer).
- **The Target** — the story you'll run the audit on.

Then you'll add a deliberately broken Apex trigger so the audit has a real component to flag, and two validation rules that enforce the gate.

### A. Anonymous Apex Window — Create Demo Stories

In **Setup → Developer Console → Debug → Open Execute Anonymous Window**, paste and execute:

```apex
Id userStoryRtId = Schema.SObjectType.copado__User_Story__c
    .getRecordTypeInfosByName().get('User Story').getRecordTypeId();

// 1. Create the Historical Failure (The Ghost)
copado__User_Story__c histGhost = new copado__User_Story__c(
    RecordTypeId = userStoryRtId,
    copado__Status__c = 'Cancelled',
    copado__Has_Apex_Code__c = true,
    copado__User_Story_Title__c = 'Old Case Trigger Logic (Historical Failure)',
    copado__UserStory_Role__c = 'Developer',
    copado__UserStory_Need__c = 'to update the old case logic',
    copado__UserStory_reason__c = 'so we can escalate cases.',
    copado__Acceptance_Criteria__c = '- Case triggers correctly.',
    copado__Cancellation_Reason__c = 'SOQL query inside for-loop caused governor limit failure during bulk escalation.'
);
insert histGhost;

// 2. Create the Active Collision (The Parallel Developer)
copado__User_Story__c activeCollision = new copado__User_Story__c(
    RecordTypeId = userStoryRtId,
    copado__Status__c = 'In Progress',
    copado__Has_Apex_Code__c = true,
    copado__User_Story_Title__c = 'Update Case Escalation Logic (Active Collision)',
    copado__UserStory_Role__c = 'Developer',
    copado__UserStory_Need__c = 'to refactor the case escalation',
    copado__UserStory_reason__c = 'so support can work faster.',
    copado__Acceptance_Criteria__c = '- Escalations work.'
);
insert activeCollision;

// 3. Create the Target Demo Story (what you will audit)
copado__User_Story__c targetStory = new copado__User_Story__c(
    RecordTypeId = userStoryRtId,
    copado__Status__c = 'Draft',
    copado__Has_Apex_Code__c = true,
    copado__User_Story_Title__c = 'Implement Case Trigger for escalation',
    copado__UserStory_Role__c = 'Release Manager',
    copado__UserStory_Need__c = 'to implement a new Case Trigger',
    copado__UserStory_reason__c = 'so we can automate the escalation process.',
    copado__Acceptance_Criteria__c = '- Case trigger processes 200+ records.\n- Test coverage > 75%.',
    copado__Functional_Specifications__c = '<p>Implement the new Case Trigger for escalations.</p>',
    copado__Technical_Specifications__c = '<p>Ensure no SOQL queries are inside for-loops.</p>',
    copado__Story_Points_SFDC__c = 5
);
insert targetStory;

// 4. Attach Metadata to all three stories
List<copado__User_Story_Metadata__c> metas = new List<copado__User_Story_Metadata__c>();

metas.add(new copado__User_Story_Metadata__c(
    copado__User_Story__c = histGhost.Id,
    copado__Type__c = 'ApexTrigger',
    copado__Metadata_API_Name__c = 'CaseTrigger'));

metas.add(new copado__User_Story_Metadata__c(
    copado__User_Story__c = activeCollision.Id,
    copado__Type__c = 'ApexTrigger',
    copado__Metadata_API_Name__c = 'CaseTrigger'));

metas.add(new copado__User_Story_Metadata__c(
    copado__User_Story__c = targetStory.Id,
    copado__Type__c = 'ApexTrigger',
    copado__Metadata_API_Name__c = 'CaseTrigger'));

metas.add(new copado__User_Story_Metadata__c(
    copado__User_Story__c = targetStory.Id,
    copado__Type__c = 'ApexClass',
    copado__Metadata_API_Name__c = 'CaseHandler'));

insert metas;

System.debug('✅ High Risk Demo Scenario Created! Target Story: ' + targetStory.Name);
```

> 💡 If `copado__Cancellation_Reason__c` is not a field on `copado__User_Story__c` in your org, either add it or remove that line. The audit falls back to `"Not recorded"` — which triggers a **different** guardrail path (INCOMPLETE-adjacent, honest about unknown root cause).

After executing, note the **Target Story Name** printed in the debug log. That's the `--story` value for your audit.

### B. Apex Trigger — The "Bad Trigger" for the Demo

This trigger is **deliberately wrong** (SOQL inside a for-loop). It exists so the audit has a real, deployed component to detect — the "DNA overlap" with the ghost story.

**File:** `CaseEscalationTrigger.trigger`

```apex
trigger CaseEscalationTrigger on Case (after update) {
    for (Case c : Trigger.new) {
        // Only act when status changes to Escalated
        if (c.Status == 'Escalated' && Trigger.oldMap.get(c.Id).Status != 'Escalated') {

            // ISSUE: SOQL query inside the loop
            if (c.ContactId != null) {
                Contact con = [SELECT Id, Email FROM Contact WHERE Id = :c.ContactId LIMIT 1];
                if (con.Email != null) {
                    System.debug('Escalation notice needed for: ' + con.Email);
                    // (Email sending logic would go here, ideally outside the loop)
                }
            }
        }
    }
}
```

**How to deploy it:**

1. **Setup → Developer Console → File → New → Apex Trigger**
2. Name: `CaseEscalationTrigger`
3. sObject: `Case`
4. Paste the code above
5. Save

> 🚀 **Do not use this in production.** It's a teaching artifact for the demo. The whole point of DeployDNA is to catch this exact pattern before it ships.

### C. Validation Rules — Block Promotion

Two validation rules on **`copado__User_Story__c`** enforce the DeployDNA gate directly in Salesforce. They work alongside the Apex webhook: the webhook runs the audit; the validation rules prevent anyone from advancing the story while a bad verdict is in place.

> **Adjust the status values** to match your Copado pipeline. The formulas below assume a common set: `Draft`, `In Progress`, `Approved`, `Ready for Deployment`, `Deploying`, `Deployed`. Replace with your org's actual `copado__Status__c` picklist values.

#### Rule 1 — Block Promotion While Analysis Is In Progress

**Setup → Object Manager → User Story → Validation Rules → New**

| Field | Value |
|---|---|
| Rule Name | `Block_Promotion_While_Analyzing` |
| Description | Prevents advancing a story while DeployDNA is still analyzing. Wait for a real verdict. |
| Error Condition Formula | *(below)* |
| Error Message | `DeployDNA is still analyzing this story. Wait for the verdict before promoting.` |
| Error Location | Top of Page |

**Formula:**

```apex
AND(
    ISCHANGED(copado__Status__c),
    TEXT(Risk_Score__c) = 'ANALYZING',
    OR(
        TEXT(copado__Status__c) = 'Approved',
        TEXT(copado__Status__c) = 'Ready for Deployment',
        TEXT(copado__Status__c) = 'Deploying',
        TEXT(copado__Status__c) = 'Deployed'
    )
)
```

**What it does:** If someone tries to move the story to any promotion-related status while `Risk_Score__c = 'ANALYZING'`, the save is blocked.

---

#### Rule 2 — Block Promotion If Risk Is High or Medium

**Setup → Object Manager → User Story → Validation Rules → New**

| Field | Value |
|---|---|
| Rule Name | `Block_Promotion_If_High_Or_Medium_Risk` |
| Description | Prevents advancing a story whose DeployDNA verdict is HIGH or MEDIUM. Resolve findings first. |
| Error Condition Formula | *(below)* |
| Error Message | `DeployDNA flagged this story as HIGH or MEDIUM risk. Resolve the findings before promoting (see attached PDF).` |
| Error Location | Top of Page |

**Formula:**

```apex
AND(
    ISCHANGED(copado__Status__c),
    OR(
        TEXT(Risk_Score__c) = 'HIGH',
        TEXT(Risk_Score__c) = 'MEDIUM'
    ),
    OR(
        TEXT(copado__Status__c) = 'Approved',
        TEXT(copado__Status__c) = 'Ready for Deployment',
        TEXT(copado__Status__c) = 'Deploying',
        TEXT(copado__Status__c) = 'Deployed'
    )
)
```

**What it does:** If the verdict is `HIGH` or `MEDIUM` and someone tries to advance to a promotion status, the save is blocked. The owner must fix the risk (or reset the score) before proceeding.

---

#### Rule 3 (recommended) — Block If Verdict Never Ran

**Rule Name:** `Block_Promotion_If_Verdict_Missing`

```apex
AND(
    ISCHANGED(copado__Status__c),
    OR(
        TEXT(copado__Status__c) = 'Approved',
        TEXT(copado__Status__c) = 'Ready for Deployment',
        TEXT(copado__Status__c) = 'Deploying',
        TEXT(copado__Status__c) = 'Deployed'
    ),
    OR(
        ISBLANK(TEXT(Risk_Score__c)),
        TEXT(Risk_Score__c) = 'INCOMPLETE'
    )
)
```

**Error Message:** `This story has no completed DeployDNA verdict. Run the audit before promoting.`

> `INCOMPLETE` is **not** the same as `CLEAR`. This rule fails closed — a broken audit can't be treated as a pass.

---

#### Quick test of the gates

After creating all three rules, in Anonymous Apex:

```apex
// Find the target story from step A
copado__User_Story__c target = [
    SELECT Id, Name, Risk_Score__c, copado__Status__c
    FROM copado__User_Story__c
    WHERE Name = 'US-0000123'   // ← use the name printed by step A
    LIMIT 1
];

// 1. Should be blocked — Risk_Score__c is 'ANALYZING' (or null)
target.copado__Status__c = 'Approved';
update target;   // ← this will throw a validation error, as intended

// 2. Clear the risk, then try again — should succeed
target.Risk_Score__c = null;
target.copado__Status__c = 'Draft';
update target;
```

---

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `sf: command not found` | SF CLI not on PATH | Reinstall SF CLI, restart shell |
| `Active SF CLI session for 'copado-trial' not found` | Not logged in | `sf org login web --alias copado-trial` |
| `COPADO_PAT / COPADO_ORG_ID / COPADO_WORKSPACE_ID are not set.` | Missing `.env` | Create `.env` in project root, restart server |
| Audit returns `INCOMPLETE` with `org read not confirmed` | Agentia lacks org-read tools | Set `DEPLOYDNA_EVIDENCE=soql` |
| Audit returns `INCOMPLETE` with `Agentia cited N historical stories not present in the evidence` | Agentia hallucinated — the guardrail caught it | Re-run; if persistent, set `DEPLOYDNA_EVIDENCE=soql` |
| Webhook returns `202 skipped — audited moments ago` | Cooldown active | Wait `DEPLOYDNA_COOLDOWN` seconds, or pass `&force=1` |
| Webhook returns `404` from Salesforce | Remote Site Setting URL doesn't match tunnel | Edit the Remote Site Setting — host only, no path |
| Callout fails silently (`System.debug` only) | Tunnel URL changed | Restart `cloudflared`, update Remote Site + Apex class |
| `Risk_Score__c` stuck on `ANALYZING` after a crash | Server crashed mid-audit | `audit_story` resets it to `INCOMPLETE` in `except BaseException`, but a hard kill won't. Manually update the story. |
| Validation rule fires on the **first** save of a new story | `ISCHANGED()` is true from null → value | Add `NOT(ISNEW())` to the formula if you want to allow initial creation |
| PDF not attached | SF CLI lacks Files permission | Verify user has `ContentDocumentLink` create access |
| `Could not parse Agentia's response` | Agentia returned non-JSON | Set `DEPLOYDNA_DEBUG=1` to see the raw prompt; check Agentia workspace tools |

### Inspecting the prompt

Set `DEPLOYDNA_DEBUG=1` before running — the full prompt is printed to stderr:

```bash
DEPLOYDNA_DEBUG=1 python deploydna.py --story US-0000123 --org copado-trial --evidence soql
```

---

## 12. Quick Reference

### One-time setup

```bash
pip install fpdf2 requests python-dotenv flask flask-cors
sf org login web --alias copado-trial
# create .env with COPADO_PAT, COPADO_ORG_ID, COPADO_WORKSPACE_ID
```

### Every session — three terminals

**Terminal 1 — Flask server:**
```bash
python deploydna_server.py
```

**Terminal 2 — Cloudflare Tunnel:**
```bash
cloudflared tunnel --url http://localhost:5000
# Copy the https://*.trycloudflare.com URL
```

**Terminal 3 — Salesforce setup (when URL changes):**
1. Update Remote Site Setting → save.
2. Update `DeployDNAWebhook.makeCallout()` `setEndpoint()` → deploy.
3. Run test: `DeployDNAWebhook.triggerFromLwc('<storyId>');`

### The three commands you'll use most

```bash
# 1. Audit a story locally (writes verdict + PDF)
python deploydna.py --story US-0000123 --org copado-trial --writeback

# 2. CI gate — fails the pipeline on HIGH or INCOMPLETE
python deploydna.py --story US-0000123 --org copado-trial --gate

# 3. Check the server is alive
curl http://localhost:5000/health
```

### Verdict cheat sheet

| Verdict | Meaning | Promotion blocked? |
|---|---|---|
| `ANALYZING` | Audit running | ✅ (Rule 1) |
| `HIGH` | Component collision or direct historical overlap | ✅ (Rule 2) |
| `MEDIUM` | Structural similarity to a past failure | ✅ (Rule 2) |
| `LOW` | Minor similarity, or verified "cure" story | ❌ — but review the PDF |
| `CLEAR` | No matches, no collisions | ❌ |
| `INCOMPLETE` | Audit couldn't finish — **not a pass** | ✅ (Rule 3) |

---

*DeployDNA — Author: Nivaditha Ramakrishnan — Copado Agentia Headless Hackathon, October 2026*