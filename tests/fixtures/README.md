Riverbed reference data for the contract tests. These are the only proof the
code matches real Riverbed APIs, because the live systems are air-gapped.

- `aternity_metadata_v2.xml`: a real Aternity OData API 2.0 `$metadata`
  (schema only, no tenant data), published at
  https://github.com/phanak-sap/pyodata-issue-files (issue #249).
- `netim_metric_data.json`: a Network Metric Data reply in the layout that
  Riverbed's official SDK reads (`_get_metric_dict_list_from_metric_data` in
  https://github.com/riverbed/steelscript-netim, steelscript/netim/core/netim.py).
  The values are made up; the keys and nesting are the SDK's.
