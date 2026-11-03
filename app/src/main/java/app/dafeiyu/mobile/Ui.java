package app.dafeiyu.mobile;

import android.content.Context;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.util.TypedValue;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

final class Ui {
    private final Context context;
    Ui(Context context) { this.context = context; }
    int dim(int id) { return context.getResources().getDimensionPixelSize(id); }
    int color(int id) { return context.getColor(id); }

    LinearLayout column() {
        LinearLayout view = new LinearLayout(context);
        view.setOrientation(LinearLayout.VERTICAL);
        return view;
    }

    TextView text(String value, int size, int color, boolean bold) {
        TextView view = new TextView(context);
        view.setText(value);
        view.setTextSize(TypedValue.COMPLEX_UNIT_PX, dim(size));
        view.setTextColor(color(color));
        view.setLineSpacing(dim(R.dimen.space_small) / 2f, 1f);
        if (bold) view.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        return view;
    }

    GradientDrawable background(int color, boolean border) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(color(color));
        drawable.setCornerRadius(dim(R.dimen.corner));
        if (border) drawable.setStroke(dim(R.dimen.outline_width), color(R.color.outline));
        return drawable;
    }

    LinearLayout card(LinearLayout page, String title, String body, boolean warning) {
        LinearLayout card = column();
        int pad = dim(R.dimen.card_padding);
        card.setPadding(pad, pad, pad, pad);
        card.setBackground(background(warning ? R.color.warning_soft : R.color.surface, !warning));
        TextView heading = text(title, R.dimen.heading_text, warning ? R.color.warning : R.color.ink, true);
        card.addView(heading);
        if (!body.isEmpty()) {
            TextView content = text(body, R.dimen.body_text, R.color.muted, false);
            add(card, content, R.dimen.space_small);
        }
        add(page, card, R.dimen.space_medium);
        return card;
    }

    void add(LinearLayout parent, View child, int topSpace) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2);
        params.topMargin = dim(topSpace);
        parent.addView(child, params);
    }

    Button button(String text, Runnable action) {
        Button button = new Button(context);
        button.setText(text);
        button.setAllCaps(false);
        button.setMinHeight(dim(R.dimen.touch_height));
        button.setOnClickListener(view -> action.run());
        return button;
    }
}
