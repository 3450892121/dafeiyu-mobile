package app.dafeiyu.mobile;

import android.content.Context;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

final class Catalogue {
    static final class Feature {
        final String name, status, module, detail;
        final boolean optional;
        Feature(JSONObject data, JSONObject labels) throws Exception {
            name = data.getString("name");
            module = data.getString("module");
            status = labels.getString(data.getString("status"));
            detail = data.optString("omissionReason", "");
            optional = data.getString("requirement").equals("optional_if_infeasible");
        }
    }

    final List<Feature> features;
    final int verified, required, optional;

    Catalogue(Context context) throws Exception {
        JSONObject root;
        try (InputStream input = context.getAssets().open("features.json");
             ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[4096];
            for (int read; (read = input.read(buffer)) != -1;) output.write(buffer, 0, read);
            root = new JSONObject(new String(output.toByteArray(), StandardCharsets.UTF_8));
        }
        JSONArray rows = root.getJSONArray("features");
        JSONObject labels = root.getJSONObject("statusLabels");
        List<Feature> result = new ArrayList<>();
        int done = 0, needed = 0, allowed = 0;
        for (int i = 0; i < rows.length(); i++) {
            JSONObject row = rows.getJSONObject(i);
            Feature feature = new Feature(row, labels);
            result.add(feature);
            if (row.getString("status").equals("verified")) done++;
            if (feature.optional) allowed++; else needed++;
        }
        features = Collections.unmodifiableList(result);
        verified = done;
        required = needed;
        optional = allowed;
    }
}
