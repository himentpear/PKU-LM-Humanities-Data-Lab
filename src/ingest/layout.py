"""Geometric reading order shared by native PDF and OCR text."""


def order_lines(lines):
    if len(lines)<6:
        return sorted(lines,key=lambda line:(line.y_top_pt,line.x_pt))
    left=min(l.x_pt for l in lines);right=max(l.x_pt+l.w_pt for l in lines)
    middle=(left+right)/2
    left_lines=[l for l in lines if l.x_pt+l.w_pt<middle-5]
    right_lines=[l for l in lines if l.x_pt>middle+5]
    spanning=[l for l in lines if l not in left_lines and l not in right_lines]
    if len(left_lines)>=3 and len(right_lines)>=3 and len(spanning)<=3:
        top=min(l.y_top_pt for l in left_lines+right_lines)
        bottom=max(l.y_top_pt+l.h_pt for l in left_lines+right_lines)
        if all(l.y_top_pt<top or l.y_top_pt>=bottom for l in spanning):
            return (sorted([l for l in spanning if l.y_top_pt<top],key=lambda l:l.y_top_pt)
                +order_lines(left_lines)+order_lines(right_lines)
                +sorted([l for l in spanning if l.y_top_pt>=bottom],key=lambda l:l.y_top_pt))
    return sorted(lines,key=lambda line:(round(line.y_top_pt/4),line.x_pt))
