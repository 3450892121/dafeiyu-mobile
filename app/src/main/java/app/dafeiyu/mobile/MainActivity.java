package app.dafeiyu.mobile;

import android.app.Activity;
import android.app.ActivityManager;
import android.os.Build;
import android.os.Bundle;
import android.os.StatFs;
import android.text.format.Formatter;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import java.util.Arrays;

public final class MainActivity extends Activity {
    private Ui ui;
    private Catalogue catalogue;
    private LinearLayout content;
    private final Button[] tabs = new Button[3];
    private int selected = 0;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        ui = new Ui(this);
        try { catalogue = new Catalogue(this); } catch (Exception ignored) { catalogue = null; }
        if (state != null) selected = state.getInt("selected", 0);

        LinearLayout root = ui.column();
        root.setBackgroundColor(ui.color(R.color.background));
        root.setFitsSystemWindows(true);
        LinearLayout header = new LinearLayout(this);
        header.setGravity(Gravity.CENTER_VERTICAL);
        int pad = ui.dim(R.dimen.page_padding);
        header.setPadding(pad, pad, pad, ui.dim(R.dimen.space_small));
        ImageView fish = new ImageView(this);
        fish.setImageResource(R.drawable.ic_fish);
        header.addView(fish, new LinearLayout.LayoutParams(ui.dim(R.dimen.icon_size), ui.dim(R.dimen.icon_size)));
        LinearLayout titles = ui.column();
        titles.setPadding(ui.dim(R.dimen.space_medium), 0, 0, 0);
        titles.addView(ui.text(getString(R.string.app_name), R.dimen.title_text, R.color.ink, true));
        titles.addView(ui.text(getString(R.string.tagline), R.dimen.caption_text, R.color.muted, false));
        header.addView(titles, new LinearLayout.LayoutParams(0, -2, 1));
        root.addView(header);

        LinearLayout navigation = new LinearLayout(this);
        navigation.setPadding(pad, 0, pad, 0);
        int[] labels = {R.string.overview, R.string.features, R.string.device};
        for (int i = 0; i < tabs.length; i++) {
            final int index = i;
            tabs[i] = ui.button(getString(labels[i]), () -> showPage(index));
            navigation.addView(tabs[i], new LinearLayout.LayoutParams(0, -2, 1));
        }
        root.addView(navigation);
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        content = ui.column();
        content.setPadding(pad, 0, pad, pad);
        scroll.addView(content);
        root.addView(scroll, new LinearLayout.LayoutParams(-1, 0, 1));
        setContentView(root);
        showPage(selected);
    }

    @Override public void onSaveInstanceState(Bundle state) {
        state.putInt("selected", selected);
        super.onSaveInstanceState(state);
    }

    private void showPage(int index) {
        selected = Math.max(0, Math.min(index, tabs.length - 1));
        content.removeAllViews();
        for (int i = 0; i < tabs.length; i++) {
            tabs[i].setTextColor(ui.color(i == selected ? R.color.accent : R.color.muted));
            tabs[i].setSelected(i == selected);
        }
        if (selected == 1) showFeatures();
        else if (selected == 2) showDevice();
        else showOverview();
    }

    private void showOverview() {
        ui.card(content, getString(R.string.phase), getString(R.string.phase_detail), true);
        // No engine is bundled in the initialization build. Never infer a live QQ session.
        Readiness.State readiness = Readiness.evaluate(false, false, false, false);
        if (readiness == Readiness.State.ENGINE_MISSING)
            ui.card(content, getString(R.string.runtime_missing), getString(R.string.runtime_detail), false);
        if (catalogue != null) {
            LinearLayout progress = ui.card(content,
                getString(R.string.progress_format, catalogue.verified, catalogue.features.size()),
                getString(R.string.required_count_format, catalogue.required, catalogue.optional), false);
            ui.add(progress, ui.button(getString(R.string.catalogue_title), () -> showPage(1)), R.dimen.space_small);
        } else ui.card(content, getString(R.string.catalogue_title), getString(R.string.catalogue_error), true);
        ui.card(content, getString(R.string.identity_title), getString(R.string.identity_detail), false);
        ui.card(content, getString(R.string.network_title), getString(R.string.network_detail), false);
        String version;
        try { version = getPackageManager().getPackageInfo(getPackageName(), 0).versionName; }
        catch (Exception ignored) { version = "—"; }
        TextView footer = ui.text(getString(R.string.version_format, version), R.dimen.caption_text, R.color.muted, false);
        footer.setGravity(Gravity.CENTER);
        ui.add(content, footer, R.dimen.space_large);
    }

    private void showFeatures() {
        ui.card(content, getString(R.string.catalogue_title), getString(R.string.catalogue_detail), false);
        if (catalogue == null) {
            ui.card(content, getString(R.string.catalogue_error), "", true);
            return;
        }
        for (Catalogue.Feature feature : catalogue.features) {
            String detail = feature.status + " · " + getString(feature.optional ? R.string.optional : R.string.required);
            ui.card(content, feature.name, detail, false);
        }
    }

    private void showDevice() {
        LinearLayout card = ui.card(content, getString(R.string.device_title), getString(R.string.device_detail), false);
        String[] lines = {
            getString(R.string.android_format, Build.VERSION.RELEASE, Build.VERSION.SDK_INT),
            getString(R.string.cpu_format, String.join(" / ", Build.SUPPORTED_ABIS)),
            Arrays.asList(Build.SUPPORTED_ABIS).contains("arm64-v8a") ? getString(R.string.arm64_yes) : getString(R.string.arm64_no)
        };
        for (String line : lines) addDetail(card, line);
        ActivityManager manager = (ActivityManager) getSystemService(ACTIVITY_SERVICE);
        if (manager != null) {
            ActivityManager.MemoryInfo info = new ActivityManager.MemoryInfo();
            manager.getMemoryInfo(info);
            addDetail(card, getString(R.string.memory_format, Formatter.formatFileSize(this, info.availMem), Formatter.formatFileSize(this, info.totalMem)));
        }
        StatFs storage = new StatFs(getFilesDir().getAbsolutePath());
        addDetail(card, getString(R.string.storage_format, Formatter.formatFileSize(this, storage.getAvailableBytes())));
        ui.add(card, ui.button(getString(R.string.check_again), () -> showPage(2)), R.dimen.space_medium);
        ui.card(content, getString(R.string.runtime_title), getString(R.string.device_disclaimer), true);
    }

    private void addDetail(LinearLayout parent, String line) {
        ui.add(parent, ui.text(line, R.dimen.body_text, R.color.ink, false), R.dimen.space_medium);
    }
}
